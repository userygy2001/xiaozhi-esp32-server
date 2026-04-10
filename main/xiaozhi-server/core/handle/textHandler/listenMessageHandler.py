import time
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__


class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器

    单一开关来源：ConnectionHandler.enable_realtime_bridge
    只需在 core.connection 里修改 CONVERSATION_MODE。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        use_realtime = getattr(conn, "enable_realtime_bridge", False)

        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )

        if msg_json["state"] == "start":
            conn.reset_audio_states()

            if use_realtime:
                conn.logger.info("[bridge] start_session")
                await conn.realtime_bridge.start_session(conn, msg_json)
                return

        elif msg_json["state"] == "stop":
            conn.client_voice_stop = True

            if use_realtime:
                conn.logger.info("[bridge] stop_session")
                await conn.realtime_bridge.stop_session(conn, msg_json)
                return

            if conn.asr.interface_type == InterfaceType.STREAM:
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)

        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()

            if use_realtime:
                conn.logger.info("[bridge] ignore detect in realtime mode")
                return

            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                is_wakeup_words = filtered_text in conn.config.get("wakeup_words")
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    enqueue_asr_report(conn, "嘿，你好呀", [])
                    await startToChat(conn, "嘿，你好呀")
                else:
                    conn.just_woken_up = True
                    enqueue_asr_report(conn, original_text, [])
                    await startToChat(conn, original_text)
