import asyncio
import uuid
from dataclasses import dataclass
from typing import Dict, Any

import websockets
from websockets.exceptions import ConnectionClosed

from core.realtime_bridge.base import BaseRealtimeClient
from . import protocol


@dataclass
class DoubaoSession:
    ws: any
    connect_id: str
    session_id: str
    started: bool = False
    closed: bool = False


class DoubaoRealtimeClient(BaseRealtimeClient):
    def __init__(self, config: dict):
        self.root_config = config or {}
        self.config = self.root_config.get("doubao", {}) or {}

        self.url = self.config.get(
            "url",
            "wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
        )

        # 调试阶段：直接把默认值写在代码里
        # 从配置文件中读取秘钥，不提供硬编码的默认值
        self.app_id = str(self.config.get("app_id", ""))
        self.access_key = str(self.config.get("access_key", ""))
        self.resource_id = str(self.config.get("resource_id", "volc.speech.dialog")) # 这个是火山引擎的通用资源ID，不是私密秘钥，保留没关系
        self.app_key = str(self.config.get("app_key", ""))
        
        # 增加一个安全检查（可选），如果没读到秘钥，在终端报错提醒自己
        if not self.app_id or not self.access_key:
            print("警告：未在配置文件中找到 Doubao 秘钥！")

        # 1.2.1.1 = O2.0
        # 2.2.0.0 = SC2.0
        self.model = str(self.config.get("model", "1.2.1.1"))

        # 通用字段
        self.speaker = self.config.get("speaker", "zh_female_xiaohe_jupiter_bigtts")
        self.dialog_id = self.config.get("dialog_id", "")
        self.input_mod = self.config.get("input_mod", "keep_alive")

        # O 版本字段
        self.bot_name = self.config.get("bot_name")
        self.system_role = self.config.get("system_role")
        self.speaking_style = self.config.get("speaking_style")

        # SC 版本字段
        self.character_manifest = self.config.get("character_manifest")

        # 上行：直接声明小智的 opus 输入
        self.asr_audio_info = self.config.get(
            "asr_audio_info",
            {
                "format": "speech_opus",
                "sample_rate": 16000,
                "channel": 1,
            },
        )

        # 下行：默认不请求 PCM，让豆包返回默认 OGG-Opus
        # 注意：loudness_rate / speech_rate 这类能力主要是 2.0 版本支持
        if self.model == "2.2.0.0":
            self.tts_audio_config = self.config.get(
                "tts_audio_config",
                {
                    "loudness_rate": 70
                },
            )
        else:
            self.tts_audio_config = self.config.get("tts_audio_config", {})

        # 其他能力开关
        self.enable_music = bool(self.config.get("enable_music", True))
        self.enable_loudness_norm = bool(self.config.get("enable_loudness_norm", False))
        self.enable_conversation_truncate = bool(
            self.config.get("enable_conversation_truncate", True)
        )
        self.enable_user_query_exit = bool(
            self.config.get("enable_user_query_exit", False)
        )

    async def connect(self, conn):
        self._validate_config()

        connect_id = str(uuid.uuid4())
        client_session_id = str(uuid.uuid4())

        headers = {
            "X-Api-App-ID": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-App-Key": self.app_key,
            "X-Api-Connect-Id": connect_id,
        }

        conn.logger.info("[doubao] connecting upstream")
        ws = await self._open_ws(headers)

        logid = None
        try:
            logid = ws.response_headers.get("X-Tt-Logid")
        except Exception:
            pass

        conn.logger.info(f"[doubao] upstream connected logid={logid}")

        session = DoubaoSession(
            ws=ws,
            connect_id=connect_id,
            session_id=client_session_id,
        )

        start_conn_frame = protocol.build_full_request_frame(
            event=protocol.EVENT_START_CONNECTION,
            payload_obj={},
            session_id=None,
            connect_id=None,
            gzip_payload=True,
        )
        await ws.send(start_conn_frame)
        start_conn_resp = protocol.parse_response_frame(await ws.recv())
        conn.logger.info(f"[doubao] StartConnection ok: {start_conn_resp.get('event')}")

        start_session_payload = self._build_start_session_payload()
        start_session_frame = protocol.build_full_request_frame(
            event=protocol.EVENT_START_SESSION,
            payload_obj=start_session_payload,
            session_id=client_session_id,
            connect_id=None,
            gzip_payload=True,
        )
        await ws.send(start_session_frame)
        start_session_resp = protocol.parse_response_frame(await ws.recv())
        conn.logger.info(f"[doubao] StartSession ok: {start_session_resp.get('event')}")

        server_session_id = start_session_resp.get("session_id")
        if not server_session_id:
            raise ValueError(f"StartSession 未返回 session_id: {start_session_resp}")

        session.session_id = server_session_id
        conn.logger.info(f"[doubao] use server session_id={session.session_id}")

        session.started = True
        return session

    async def send_audio(self, conn, provider: DoubaoSession, audio_bytes: bytes):
        if not audio_bytes or provider.closed:
            return

        frame = protocol.build_audio_request_frame(
            event=protocol.EVENT_TASK_REQUEST,
            audio_bytes=audio_bytes,
            session_id=provider.session_id,
            sequence_flag=protocol.NO_SEQUENCE,
            gzip_payload=False,
        )
        await provider.ws.send(frame)

    async def send_text(self, conn, provider: DoubaoSession, text: str):
        if not text or provider.closed:
            return

        payload = {
            "content": text,
        }
        frame = protocol.build_full_request_frame(
            event=protocol.EVENT_CHAT_TEXT_QUERY,
            payload_obj=payload,
            session_id=provider.session_id,
            gzip_payload=True,
        )
        await provider.ws.send(frame)

    async def commit_input(self, conn, provider: DoubaoSession):
        # 先保持 noop，不把每一轮 stop 直接映射成 FinishSession
        return

    async def recv_event(self, conn, provider: DoubaoSession):
        try:
            raw = await asyncio.wait_for(provider.ws.recv(), timeout=8)
        except asyncio.TimeoutError:
            return None
        except ConnectionClosed as e:
            provider.closed = True
            return {
                "type": "error",
                "code": e.code,
                "message": e.reason or "upstream closed",
            }

        frame = protocol.parse_response_frame(raw)
        message_type = frame.get("message_type")

        if message_type == "SERVER_ERROR_RESPONSE":
            conn.logger.info(
                f"[doubao] error response code={frame.get('code')} payload={frame.get('payload_msg')}"
            )
            return {
                "type": "error",
                "code": frame.get("code"),
                "message": frame.get("payload_msg"),
            }

        if message_type == "SERVER_AUDIO_ONLY_RESPONSE":
            event_id = frame.get("event")
            audio_bytes = frame.get("payload_msg", b"") or b""
            is_tail = bool(frame.get("is_tail", False))

            # conn.logger.info(
            #     f"[doubao] got audio-only response event={event_id} bytes={len(audio_bytes)} tail={is_tail}"
            # )

            # 官方文档：352 = TTSResponse（音频块）
            if event_id == 352 and audio_bytes:
                return {
                    "type": "audio_ogg_opus.delta",
                    "data": audio_bytes,
                }

            return None

        if message_type == "SERVER_FULL_RESPONSE":
            event_id = frame.get("event")
            payload = frame.get("payload_msg") or {}

            # conn.logger.info(f"[doubao] full response event_id={event_id}")

            # ASR 开始
            if event_id == 450:
                return None

            # 用户语音识别中间/最终文本
            if event_id == 451:
                results = payload.get("results") or []
                if results:
                    first = results[0]
                    text = (first.get("text") or "").strip()
                    is_interim = bool(first.get("is_interim", False))
                    if text:
                        return {
                            "type": "user_text",
                            "text": text,
                            "is_final": not is_interim,
                        }
                return None

            # 回复文本
            if event_id == 550:
                text = (payload.get("content") or "").strip()
                if text:
                    return {
                        "type": "assistant_text",
                        "text": text,
                    }
                return None

            # 官方文档：359 = TTSEnded
            if event_id == 359:
                return {"type": "audio.done"}

            # 如果 payload 里带 question_id / reply_id，作为“回复开始”的信号
            if payload.get("question_id") or payload.get("reply_id"):
                return {
                    "type": "reply_started",
                    "question_id": payload.get("question_id"),
                    "reply_id": payload.get("reply_id"),
                }

            return None

        return None

    async def close(self, conn, provider: DoubaoSession):
        if provider.closed:
            return

        provider.closed = True

        try:
            finish_session_frame = protocol.build_full_request_frame(
                event=protocol.EVENT_FINISH_SESSION,
                payload_obj={},
                session_id=provider.session_id,
                gzip_payload=True,
            )
            await provider.ws.send(finish_session_frame)
        except Exception as e:
            conn.logger.warning(f"[doubao] FinishSession send failed: {e}")

        try:
            finish_connection_frame = protocol.build_full_request_frame(
                event=protocol.EVENT_FINISH_CONNECTION,
                payload_obj={},
                session_id=None,
                gzip_payload=True,
            )
            await provider.ws.send(finish_connection_frame)
        except Exception as e:
            conn.logger.warning(f"[doubao] FinishConnection send failed: {e}")

        try:
            await provider.ws.close()
        except Exception as e:
            conn.logger.warning(f"[doubao] ws close failed: {e}")

    def _build_start_session_payload(self) -> Dict[str, Any]:
        dialog_extra: Dict[str, Any] = {
            "enable_music": self.enable_music,
            "enable_loudness_norm": self.enable_loudness_norm,
            "enable_conversation_truncate": self.enable_conversation_truncate,
            "enable_user_query_exit": self.enable_user_query_exit,
            "model": self.model,
        }

        if self.input_mod:
            dialog_extra["input_mod"] = self.input_mod

        payload: Dict[str, Any] = {
            "asr": {
                "audio_info": self.asr_audio_info,
                "extra": {},
            },
            "tts": {
                "extra": {},
            },
            "dialog": {
                "dialog_id": self.dialog_id,
                "extra": dialog_extra,
            },
        }

        # 只有显式配置了 tts_audio_config 时，才强制指定下行格式
        # 不配置时让豆包走默认下行（OGG 封装 Opus）
        if self.tts_audio_config:
            payload["tts"]["audio_config"] = self.tts_audio_config

        if self.speaker:
            payload["tts"]["speaker"] = self.speaker

        if self.bot_name is not None:
            payload["dialog"]["bot_name"] = self.bot_name
        if self.system_role is not None:
            payload["dialog"]["system_role"] = self.system_role
        if self.speaking_style is not None:
            payload["dialog"]["speaking_style"] = self.speaking_style
        if self.character_manifest is not None:
            payload["dialog"]["character_manifest"] = self.character_manifest

        return payload

    async def _open_ws(self, headers: dict):
        try:
            return await websockets.connect(
                self.url,
                additional_headers=headers,
                ping_interval=None,
            )
        except TypeError:
            return await websockets.connect(
                self.url,
                extra_headers=headers,
                ping_interval=None,
            )

    def _validate_config(self):
        if not self.app_id:
            raise ValueError("realtime_bridge.doubao.app_id 未配置")
        if not self.access_key:
            raise ValueError("realtime_bridge.doubao.access_key 未配置")
        if not self.model:
            raise ValueError("realtime_bridge.doubao.model 未配置")