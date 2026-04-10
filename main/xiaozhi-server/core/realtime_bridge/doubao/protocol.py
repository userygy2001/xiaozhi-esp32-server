import gzip
import json
from typing import Any, Dict, Optional


# ===== 协议常量 =====

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message Type
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010

SERVER_FULL_RESPONSE = 0b1001
SERVER_AUDIO_ONLY_RESPONSE = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Message type specific flags
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_SEQUENCE_1 = 0b0011

MSG_WITH_EVENT = 0b0100

# Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001

# Compression
NO_COMPRESSION = 0b0000
GZIP = 0b0001


# ===== 客户端事件 ID（按官方文档 + 官方示例） =====

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2

EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_TASK_REQUEST = 200

EVENT_SAY_HELLO = 300

EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CHAT_RAG_TEXT = 502


# ===== 一些已观察到的服务端 ACK 事件（来自官方示例日志） =====
# 这里只用于日志识别，不依赖它们驱动核心逻辑

SERVER_EVENT_START_CONNECTION_ACK = 50
SERVER_EVENT_FINISH_CONNECTION_ACK = 52
SERVER_EVENT_START_SESSION_ACK = 150


def generate_header(
    version: int = PROTOCOL_VERSION,
    message_type: int = CLIENT_FULL_REQUEST,
    message_type_specific_flags: int = MSG_WITH_EVENT,
    serial_method: int = JSON,
    compression_type: int = GZIP,
    reserved_data: int = 0x00,
    extension_header: bytes = bytes(),
) -> bytearray:
    header = bytearray()
    header_size = int(len(extension_header) / 4) + 1
    header.append((version << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    header.extend(extension_header)
    return header


def build_full_request_frame(
    event: int,
    payload_obj: Dict[str, Any],
    session_id: Optional[str] = None,
    connect_id: Optional[str] = None,
    gzip_payload: bool = True,
) -> bytes:
    compression = GZIP if gzip_payload else NO_COMPRESSION
    header = bytearray(
        generate_header(
            message_type=CLIENT_FULL_REQUEST,
            message_type_specific_flags=MSG_WITH_EVENT,
            serial_method=JSON,
            compression_type=compression,
        )
    )

    header.extend(int(event).to_bytes(4, "big"))

    if connect_id is not None:
        connect_id_bytes = connect_id.encode("utf-8")
        header.extend(len(connect_id_bytes).to_bytes(4, "big"))
        header.extend(connect_id_bytes)

    if session_id is not None:
        session_id_bytes = session_id.encode("utf-8")
        header.extend(len(session_id_bytes).to_bytes(4, "big"))
        header.extend(session_id_bytes)

    payload_bytes = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    if gzip_payload:
        payload_bytes = gzip.compress(payload_bytes)

    header.extend(len(payload_bytes).to_bytes(4, "big"))
    header.extend(payload_bytes)
    return bytes(header)


def build_audio_request_frame(
    event: int,
    audio_bytes: bytes,
    session_id: str,
    sequence_flag: int = NO_SEQUENCE,
    gzip_payload: bool = False,
) -> bytes:
    compression = GZIP if gzip_payload else NO_COMPRESSION
    header = bytearray(
        generate_header(
            message_type=CLIENT_AUDIO_ONLY_REQUEST,
            message_type_specific_flags=sequence_flag | MSG_WITH_EVENT,
            serial_method=NO_SERIALIZATION,
            compression_type=compression,
        )
    )

    header.extend(int(event).to_bytes(4, "big"))

    session_id_bytes = session_id.encode("utf-8")
    header.extend(len(session_id_bytes).to_bytes(4, "big"))
    header.extend(session_id_bytes)

    payload_bytes = gzip.compress(audio_bytes) if gzip_payload else audio_bytes
    header.extend(len(payload_bytes).to_bytes(4, "big"))
    header.extend(payload_bytes)
    return bytes(header)


def _decode_payload(
    payload_msg: bytes,
    serialization_method: int,
    message_compression: int,
):
    if message_compression == GZIP:
        payload_msg = gzip.decompress(payload_msg)

    if serialization_method == JSON:
        return json.loads(payload_msg.decode("utf-8"))
    if serialization_method == NO_SERIALIZATION:
        return payload_msg
    return payload_msg.decode("utf-8", errors="ignore")


def _parse_sequence_flag(flags: int) -> int:
    # 低 2 bit 是 sequence flag，高 bit 里可能带 MSG_WITH_EVENT
    return flags & 0b0011


def parse_response_frame(res: bytes) -> Dict[str, Any]:
    if isinstance(res, str):
        return {}

    protocol_version = res[0] >> 4
    header_size = res[0] & 0x0F
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0F
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0F
    reserved = res[3]

    payload = res[header_size * 4:]

    result: Dict[str, Any] = {
        "protocol_version": protocol_version,
        "header_size": header_size,
        "message_type_raw": message_type,
        "flags_raw": message_type_specific_flags,
        "serialization_raw": serialization_method,
        "compression_raw": message_compression,
        "reserved": reserved,
    }

    start = 0
    seq_flag = _parse_sequence_flag(message_type_specific_flags)
    has_event = (message_type_specific_flags & MSG_WITH_EVENT) > 0

    # Full server response
    if message_type == SERVER_FULL_RESPONSE:
        result["message_type"] = "SERVER_FULL_RESPONSE"

        if seq_flag != NO_SEQUENCE:
            result["seq"] = int.from_bytes(payload[start:start + 4], "big", signed=True)
            start += 4

        if has_event:
            result["event"] = int.from_bytes(payload[start:start + 4], "big", signed=False)
            start += 4

        session_id_size = int.from_bytes(payload[start:start + 4], "big", signed=True)
        start += 4
        session_id = payload[start:start + session_id_size]
        start += session_id_size

        result["session_id"] = session_id.decode("utf-8", errors="ignore")

        payload_size = int.from_bytes(payload[start:start + 4], "big", signed=False)
        start += 4
        payload_msg = payload[start:start + payload_size]

        payload_msg = _decode_payload(
            payload_msg,
            serialization_method=serialization_method,
            message_compression=message_compression,
        )

        result["payload_size"] = payload_size
        result["payload_msg"] = payload_msg
        return result

    # Audio-only response
    if message_type == SERVER_AUDIO_ONLY_RESPONSE:
        result["message_type"] = "SERVER_AUDIO_ONLY_RESPONSE"

        if seq_flag != NO_SEQUENCE:
            result["seq"] = int.from_bytes(payload[start:start + 4], "big", signed=True)
            start += 4
        else:
            result["seq"] = None

        result["is_tail"] = seq_flag in (NEG_SEQUENCE, NEG_SEQUENCE_1)

        if has_event:
            result["event"] = int.from_bytes(payload[start:start + 4], "big", signed=False)
            start += 4

        # 按官方 TTSResponse 示例，audio-only response 里同样带 session id
        session_id_size = int.from_bytes(payload[start:start + 4], "big", signed=False)
        start += 4

        session_id = payload[start:start + session_id_size]
        start += session_id_size
        result["session_id"] = session_id.decode("utf-8", errors="ignore")

        payload_size = int.from_bytes(payload[start:start + 4], "big", signed=False)
        start += 4

        payload_msg = payload[start:start + payload_size]
        payload_msg = _decode_payload(
            payload_msg,
            serialization_method=serialization_method,
            message_compression=message_compression,
        )

        result["payload_size"] = payload_size
        result["payload_msg"] = payload_msg
        return result

    # Error
    if message_type == SERVER_ERROR_RESPONSE:
        result["message_type"] = "SERVER_ERROR_RESPONSE"
        code = int.from_bytes(payload[0:4], "big", signed=False)
        payload_size = int.from_bytes(payload[4:8], "big", signed=False)
        payload_msg = payload[8:8 + payload_size]

        payload_msg = _decode_payload(
            payload_msg,
            serialization_method=serialization_method,
            message_compression=message_compression,
        )

        result["code"] = code
        result["payload_size"] = payload_size
        result["payload_msg"] = payload_msg
        return result

    result["message_type"] = "UNKNOWN"
    result["payload_raw"] = payload
    return result