import asyncio
import audioop
import re
import time

from core.handle.sendAudioHandle import sendAudio, send_tts_message
from core.utils.ogg_opus_native import OggOpusNative
from core.utils.opus_encoder_utils import OpusEncoderUtils
from .factory import create_provider_client
from .audio_adapter import (
    pcm_to_xiaozhi_opus_frames,
    audio_bytes_to_xiaozhi_frames,
)


class BridgeSession:
    def __init__(self):
        # provider / connection 级状态：尽量跨多轮保留
        self.provider = None
        self.provider_ready = False
        self.closing = False

        # 后台任务：尽量常驻
        self.audio_queue = asyncio.Queue(maxsize=200)
        self.sender_task = None
        self.recv_task = None

        # 输入期状态
        self.input_active = False
        self.input_committed = False

        # 输出期状态
        self.output_started = False
        self.reply_started = False

        # 用户字幕状态
        self.last_user_text = ""
        self.last_user_interim_text = ""

        # assistant 文本状态
        self.current_assistant_text = ""
        self.assistant_full_text = ""

        # assistant 分句状态
        self.assistant_segments = []
        self.assistant_display_index = -1
        self.assistant_last_sent_segment = ""

        # 音频时长统计（单位：秒）
        self.output_audio_duration_sec = 0.0
        
        # [新增] 记录真实世界开始播放的系统时间戳，用于精准计算字幕进度
        self.playback_start_time_sec = 0.0 

        # OGG-Opus 流式播放链路
        self.ogg_native = OggOpusNative()
        self.tts_opus_encoder = OpusEncoderUtils(16000, 1, 60)

    def reset_stream_output_state(self):
        try:
            self.ogg_native.reset()
        except Exception:
            pass
        try:
            self.tts_opus_encoder.reset_state()
        except Exception:
            pass

        self.output_audio_duration_sec = 0.0
        # [新增] 每次重置输出状态时，将真实播放时间戳归零
        self.playback_start_time_sec = 0.0
        
        self.assistant_segments = []
        self.assistant_display_index = -1
        self.assistant_last_sent_segment = ""

class RealtimeBridge:
    def __init__(self, config: dict):
        self.config = config or {}
        self.sessions = {}
        self.client = create_provider_client(self.config)

    def _get_session(self, conn):
        sid = conn.session_id or "default"
        if sid not in self.sessions:
            self.sessions[sid] = BridgeSession()
        return self.sessions[sid]

    async def _ensure_provider_ready(self, conn, session):
        if session.provider is not None and session.provider_ready:
            return

        session.provider = await self.client.connect(conn)
        session.provider_ready = True
        session.closing = False

        conn.logger.info("[bridge] provider connected")

        if session.sender_task is None or session.sender_task.done():
            session.sender_task = asyncio.create_task(
                self._audio_sender_loop(conn, session)
            )

        if session.recv_task is None or session.recv_task.done():
            session.recv_task = asyncio.create_task(
                self._recv_loop(conn, session)
            )

    def _reset_input_state(self, session):
        session.input_active = False
        session.input_committed = False
        session.output_started = False
        session.reply_started = False

        session.last_user_text = ""
        session.last_user_interim_text = ""

        session.current_assistant_text = ""
        session.assistant_full_text = ""

        session.reset_stream_output_state()

    async def start_session(self, conn, listen_msg=None):
        session = self._get_session(conn)

        conn.logger.info("[bridge] start_session")

        if session.closing or session.provider is None or not session.provider_ready:
            conn.logger.info("[bridge] provider not ready or closing, rebuild provider")

            try:
                await self.close_session(conn)
            except Exception as e:
                conn.logger.warning(f"[bridge] close stale session failed: {e}")

            await self._ensure_provider_ready(conn, session)

        if session.input_active:
            return

        if session.output_started:
            conn.logger.info("[bridge] assistant still speaking, skip new input")
            return

        while not session.audio_queue.empty():
            try:
                session.audio_queue.get_nowait()
            except Exception:
                break

        self._reset_input_state(session)
        session.input_active = True

        conn.logger.info("[bridge] input window opened on existing provider")

    async def stop_session(self, conn, listen_msg=None):
        conn.logger.info("[bridge] stop_session")
        session = self._get_session(conn)

        if session.provider is None or not session.provider_ready:
            conn.logger.info("[bridge] stop ignored: provider not ready")
            return

        if not session.input_active:
            conn.logger.info("[bridge] stop ignored: no active input window")
            return

        if session.input_committed:
            conn.logger.info("[bridge] stop ignored: already committed")
            return

        session.input_committed = True
        session.input_active = False

        await self.client.commit_input(conn, session.provider)
        conn.logger.info("[bridge] input committed")

    def enqueue_device_audio(self, conn, audio_bytes: bytes):
        session = self._get_session(conn)

        if (
            session.provider is None
            or not session.provider_ready
            or not session.input_active
            or session.input_committed
        ):
            return

        try:
            session.audio_queue.put_nowait(audio_bytes)
        except asyncio.QueueFull:
            conn.logger.warning("[bridge] audio_queue full, drop 1 frame")

    async def _audio_sender_loop(self, conn, session):
        while not session.closing:
            try:
                audio_bytes = await session.audio_queue.get()

                if (
                    session.provider is None
                    or not session.provider_ready
                    or not session.input_active
                    or session.input_committed
                ):
                    continue

                await self.client.send_audio(conn, session.provider, audio_bytes)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                conn.logger.error(f"[bridge] send audio failed: {e}")
                break

    def _pcm48_to_pcm16_mono(self, pcm_bytes: bytes, channels: int) -> bytes:
        """
        输入：
          48kHz, int16 little-endian PCM
        输出：
          16kHz, mono, int16 little-endian PCM
        """
        if not pcm_bytes:
            return b""

        if channels == 2:
            pcm_bytes = audioop.tomono(pcm_bytes, 2, 0.5, 0.5)
        elif channels != 1:
            raise ValueError(f"unsupported pcm channels: {channels}")

        converted, _ = audioop.ratecv(pcm_bytes, 2, 1, 48000, 16000, None)
        return converted

    def _seconds_from_pcm16_mono_16k(self, pcm_bytes: bytes) -> float:
        if not pcm_bytes:
            return 0.0
        # 16kHz * mono * int16 = 16000 * 2 bytes/sec
        return len(pcm_bytes) / 32000.0

    def _seconds_from_pcm(self, pcm_bytes: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> float:
        if not pcm_bytes or sample_rate <= 0 or channels <= 0 or sample_width <= 0:
            return 0.0
        return len(pcm_bytes) / float(sample_rate * channels * sample_width)

    def _split_text_to_segments(self, text: str):
        """
        统一分句策略：
        1. 优先按标点切句
        2. 无标点时按长度兜底
        """
        if not text:
            return []

        text = text.strip()
        if not text:
            return []

        # 保留空格最少化，避免英文/中文混合时错位太明显
        text = re.sub(r"[ \t]+", "", text)

        parts = []
        buf = ""

        for ch in text:
            buf += ch
            if ch in "，。！？；、\n":
                seg = buf.strip()
                if seg:
                    parts.append(seg)
                buf = ""

        if buf.strip():
            buf = buf.strip()
            # 没有句末标点时，长度兜底切
            chunk_size = 12
            while len(buf) > chunk_size:
                parts.append(buf[:chunk_size])
                buf = buf[chunk_size:]
            if buf:
                parts.append(buf)

        # 过长片段二次切分
        final_parts = []
        for seg in parts:
            seg = seg.strip()
            if not seg:
                continue

            if len(seg) <= 16:
                final_parts.append(seg)
            else:
                chunk_size = 12
                tmp = seg
                while len(tmp) > chunk_size:
                    final_parts.append(tmp[:chunk_size])
                    tmp = tmp[chunk_size:]
                if tmp:
                    final_parts.append(tmp)

        return final_parts

    def _refresh_assistant_segments(self, session):
        text = (session.assistant_full_text or "").strip()
        session.assistant_segments = self._split_text_to_segments(text)

    def _segment_weight(self, seg: str) -> float:
        seg = (seg or "").strip()
        if not seg:
            return 0.0

        w = float(len(seg))

        if seg.endswith(("。", "！", "？")):
            w += 2.0
        elif seg.endswith(("，", "；", "、")):
            w += 1.0

        return max(w, 1.0)

    def _estimate_current_segment_index(self, session):
            """
            根据真实流逝的时间与已送去播放的音频时长，近似判断当前应显示到哪一句。
            """
            segs = session.assistant_segments
            if not segs:
                return -1

            # [新增] 1. 计算现实世界真实流逝的时间
            if session.playback_start_time_sec > 0:
                real_elapsed = time.time() - session.playback_start_time_sec
            else:
                real_elapsed = 0.0

            # [新增] 2. 重点保护：如果网络卡顿，真实时间跑得比音频下载快，
            # 必须用已下载音频时长进行兜底限制，防止字幕跑到声音前面
            elapsed = min(real_elapsed, session.output_audio_duration_sec)

            if elapsed <= 0:
                return 0

            total_chars = sum(max(len(s.strip()), 1) for s in segs)

            # 经验语速：
            # - 普通说话大概 4~6 汉字/秒
            # - 唱歌通常更慢
            # 这里折中用 3.5，宁可慢一点推进
            chars_per_sec = 3.5
            estimated_total_sec = max(elapsed, total_chars / chars_per_sec)

            weights = [self._segment_weight(s) for s in segs]
            total_weight = sum(weights) or 1.0
            progress = min(1.0, elapsed / estimated_total_sec)
            target = total_weight * progress

            acc = 0.0
            for i, w in enumerate(weights):
                acc += w
                if target <= acc:
                    return i

            return len(segs) - 1

    async def _push_user_subtitle(self, conn, text: str):
        # 不再向设备发送字幕文本
        return

    async def _maybe_push_current_assistant_segment(self, conn, session, force_last: bool = False):
        # 不再向设备发送 assistant 字幕文本
        return

    async def _stream_ogg_opus_delta_to_device(self, conn, session, ogg_bytes: bytes):
        if not ogg_bytes:
            return

        try:
            pcm48_bytes, channels = session.ogg_native.feed(ogg_bytes)
        except Exception as e:
            conn.logger.error(f"[bridge] ogg feed/decode failed: {e}")
            return

        if not pcm48_bytes:
            return

        try:
            pcm16_mono = self._pcm48_to_pcm16_mono(pcm48_bytes, channels)
        except Exception as e:
            conn.logger.error(f"[bridge] pcm resample failed: {e}")
            return

        out_frames = []

        def _on_frame(frame_data: bytes):
            out_frames.append(frame_data)

        try:
            session.tts_opus_encoder.encode_pcm_to_opus_stream(
                pcm_data=pcm16_mono,
                end_of_stream=False,
                callback=_on_frame,
            )
        except Exception as e:
            conn.logger.error(f"[bridge] opus re-encode stream failed: {e}")
            return

        if out_frames:
            await self._ensure_output_started(conn, session)

            session.output_audio_duration_sec += self._seconds_from_pcm16_mono_16k(pcm16_mono)
            await self._maybe_push_current_assistant_segment(conn, session, force_last=False)
            await sendAudio(conn, out_frames)

    async def _flush_stream_tail_to_device(self, conn, session):
        """
        在 audio.done 时冲尾：
        1. 从 native 层 flush 尾部 PCM
        2. 用 end_of_stream=True 把 OpusEncoderUtils 内部不足一帧的数据补齐编码出去
        """
        tail_pcm48 = b""
        tail_channels = session.ogg_native.channels

        try:
            tail_pcm48, tail_channels = session.ogg_native.flush()
        except Exception as e:
            conn.logger.error(f"[bridge] ogg flush failed: {e}")

        tail_pcm16 = b""
        if tail_pcm48:
            try:
                tail_pcm16 = self._pcm48_to_pcm16_mono(tail_pcm48, tail_channels)
            except Exception as e:
                conn.logger.error(f"[bridge] tail pcm resample failed: {e}")

        out_frames = []

        def _on_tail(frame_data: bytes):
            out_frames.append(frame_data)

        try:
            session.tts_opus_encoder.encode_pcm_to_opus_stream(
                pcm_data=tail_pcm16,
                end_of_stream=True,
                callback=_on_tail,
            )
        except Exception as e:
            conn.logger.error(f"[bridge] opus tail encode failed: {e}")
            return

        if out_frames:
            await self._ensure_output_started(conn, session)

            session.output_audio_duration_sec += self._seconds_from_pcm16_mono_16k(tail_pcm16)
            await self._maybe_push_current_assistant_segment(conn, session, force_last=False)
            await sendAudio(conn, out_frames)

    async def _recv_loop(self, conn, session):
        while not session.closing:
            try:
                if session.provider is None or not session.provider_ready:
                    await asyncio.sleep(0.05)
                    continue

                event = await self.client.recv_event(conn, session.provider)
                if event is None:
                    continue

                event_type = event.get("type")

                if event_type == "user_text":
                    text = (event.get("text") or "").strip()
                    is_final = bool(event.get("is_final", False))

                    if not text:
                        continue

                    if is_final:
                        if text != session.last_user_text:
                            session.last_user_text = text
                            session.last_user_interim_text = text
                            conn.logger.info(f"[user] {text}")
                            await self._push_user_subtitle(conn, text)
                    else:
                        if text != session.last_user_interim_text:
                            session.last_user_interim_text = text
                            conn.logger.info(f"[user?] {text}")
                            await self._push_user_subtitle(conn, text)
                    continue

                if event_type == "reply_started":
                    if not session.reply_started:
                        session.reply_started = True
                        conn.logger.info(
                            f"[assistant] 开始回复 question_id={event.get('question_id')} reply_id={event.get('reply_id')}"
                        )
                    continue

                if event_type == "assistant_text":
                    text = (event.get("text") or "").strip()
                    if text:
                        session.current_assistant_text = text
                        session.assistant_full_text = text
                        self._refresh_assistant_segments(session)
                        conn.logger.info(f"[assistant] {text}")

                        # 如果已经开始播放了，尝试根据当前音频进度刷新“当前句”
                        if session.output_started:
                            await self._maybe_push_current_assistant_segment(conn, session, force_last=False)
                    continue

                if event_type == "audio_ogg_opus.delta":
                    data = event.get("data", b"") or b""
                    if data:
                        await self._stream_ogg_opus_delta_to_device(conn, session, data)
                    continue

                if event_type == "audio_pcm.delta":
                    await self._stream_pcm_delta_to_device(
                        conn,
                        session,
                        event["pcm"],
                        sample_rate=event.get("sample_rate", 24000),
                    )
                    continue

                if event_type == "audio.done":
                    # 先冲尾
                    await self._flush_stream_tail_to_device(conn, session)

                    # 最后强制显示最后一句，避免尾句没刷出来
                    await self._maybe_push_current_assistant_segment(conn, session, force_last=True)

                    await self._finish_output_to_device(conn, session)

                    conn.logger.info("[assistant] 回复音频结束")

                    session.input_committed = False
                    session.reply_started = False
                    session.last_user_text = ""
                    session.last_user_interim_text = ""
                    session.current_assistant_text = ""
                    session.assistant_full_text = ""
                    session.reset_stream_output_state()
                    continue

                if event_type == "error":
                    conn.logger.error(
                        f"[bridge] upstream error code={event.get('code')} msg={event.get('message')}"
                    )

                    session.input_active = False
                    session.input_committed = False
                    session.output_started = False
                    session.reply_started = False
                    session.last_user_text = ""
                    session.last_user_interim_text = ""
                    session.current_assistant_text = ""
                    session.assistant_full_text = ""
                    session.reset_stream_output_state()

                    await self.close_session(conn)
                    break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                conn.logger.error(f"[bridge] recv upstream failed: {e}")
                try:
                    await self.close_session(conn)
                except Exception:
                    pass
                break

    async def on_detect_text(self, conn, text: str):
        session = self._get_session(conn)

        if session.provider is None or not session.provider_ready:
            await self._ensure_provider_ready(conn, session)
            session = self._get_session(conn)

        await self.client.send_text(conn, session.provider, text)

    async def _ensure_output_started(self, conn, session):
            if session.output_started:
                return

            conn.client_is_speaking = True

            # 先让设备进入 Speaking 状态，但不再立刻塞整段字幕
            await send_tts_message(conn, "start", None)

            session.output_started = True
            
            # [新增] 记录设备收到第一帧数据，准备开始播放的真实世界时间
            session.playback_start_time_sec = time.time() 
            
            conn.logger.info("[assistant] 回复音频开始")

            # 如果此时已经有可分句文本，先显示第 1 句
            await self._maybe_push_current_assistant_segment(conn, session, force_last=False)

    async def _stream_pcm_delta_to_device(self, conn, session, pcm_bytes: bytes, sample_rate: int):
        if not pcm_bytes:
            return

        await self._ensure_output_started(conn, session)

        # 假定单声道、int16
        session.output_audio_duration_sec += self._seconds_from_pcm(
            pcm_bytes,
            sample_rate=sample_rate,
            channels=1,
            sample_width=2,
        )
        await self._maybe_push_current_assistant_segment(conn, session, force_last=False)

        opus_frames = pcm_to_xiaozhi_opus_frames(
            pcm_bytes,
            sample_rate=sample_rate,
        )
        if opus_frames:
            await sendAudio(conn, opus_frames)

    async def _finish_output_to_device(self, conn, session):
        if not session.output_started:
            return

        await send_tts_message(conn, "stop", None)
        conn.client_is_speaking = False
        session.output_started = False

    async def emit_pcm_to_device(self, conn, pcm_bytes: bytes, sample_rate: int, text=" "):
        opus_frames = pcm_to_xiaozhi_opus_frames(
            pcm_bytes,
            sample_rate=sample_rate,
        )
        await self.emit_opus_to_device(conn, opus_frames, text=text)

    async def emit_audio_bytes_to_device(self, conn, audio_bytes: bytes, file_type: str, text=" "):
        opus_frames = audio_bytes_to_xiaozhi_frames(
            audio_bytes,
            file_type=file_type,
            sample_rate=conn.sample_rate,
        )
        await self.emit_opus_to_device(conn, opus_frames, text=text)

    async def emit_opus_to_device(self, conn, opus_frames, text=" "):
        if not opus_frames:
            return

        conn.client_is_speaking = True
        await send_tts_message(conn, "start", None)
        # 不再向设备发送字幕文本
        await sendAudio(conn, opus_frames)
        await send_tts_message(conn, "stop", None)
        conn.client_is_speaking = False

    async def close_session(self, conn):
        session = self._get_session(conn)

        if session.closing and session.provider is None:
            return

        session.closing = True

        try:
            await self._finish_output_to_device(conn, session)
        except Exception:
            pass

        if session.provider is not None:
            try:
                await self.client.close(conn, session.provider)
            except Exception as e:
                conn.logger.warning(f"[bridge] close provider failed: {e}")

        session.provider = None
        session.provider_ready = False

        self._reset_input_state(session)

        current = asyncio.current_task()

        if session.sender_task and not session.sender_task.done() and session.sender_task is not current:
            session.sender_task.cancel()
            try:
                await session.sender_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        session.sender_task = None

        if session.recv_task and not session.recv_task.done() and session.recv_task is not current:
            session.recv_task.cancel()
            try:
                await session.recv_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        session.recv_task = None

        while not session.audio_queue.empty():
            try:
                session.audio_queue.get_nowait()
            except Exception:
                break

        session.closing = False
        conn.logger.info("[bridge] session closed")