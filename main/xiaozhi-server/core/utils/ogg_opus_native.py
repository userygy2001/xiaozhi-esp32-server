# core/utils/ogg_opus_native.py

import os
import ctypes
from ctypes import (
    c_void_p,
    c_char_p,
    c_ubyte,
    c_size_t,
    c_int,
    POINTER,
)


class OggOpusNativeError(RuntimeError):
    pass


class OggOpusNative:
    """
    libogg + libopus 的薄包装：
    - feed(ogg_bytes) -> (pcm_bytes, channels)
    - flush() -> (pcm_bytes, channels)
    - reset()
    """

    def __init__(self, lib_path: str | None = None):
        self._lib = self._load_library(lib_path)

        # create / destroy
        self._lib.ogg_opus_native_create.restype = c_void_p
        self._lib.ogg_opus_native_create.argtypes = []

        self._lib.ogg_opus_native_destroy.restype = None
        self._lib.ogg_opus_native_destroy.argtypes = [c_void_p]

        # reset
        self._lib.ogg_opus_native_reset.restype = None
        self._lib.ogg_opus_native_reset.argtypes = [c_void_p]

        # feed / flush
        self._lib.ogg_opus_native_feed.restype = c_int
        self._lib.ogg_opus_native_feed.argtypes = [c_void_p, POINTER(c_ubyte), c_size_t]

        self._lib.ogg_opus_native_flush.restype = c_int
        self._lib.ogg_opus_native_flush.argtypes = [c_void_p]

        # output access
        self._lib.ogg_opus_native_get_pcm_ptr.restype = POINTER(c_ubyte)
        self._lib.ogg_opus_native_get_pcm_ptr.argtypes = [c_void_p]

        self._lib.ogg_opus_native_get_pcm_len.restype = c_size_t
        self._lib.ogg_opus_native_get_pcm_len.argtypes = [c_void_p]

        self._lib.ogg_opus_native_clear_output.restype = None
        self._lib.ogg_opus_native_clear_output.argtypes = [c_void_p]

        # info / error
        self._lib.ogg_opus_native_get_last_error.restype = c_char_p
        self._lib.ogg_opus_native_get_last_error.argtypes = [c_void_p]

        self._lib.ogg_opus_native_get_decoder_channels.restype = c_int
        self._lib.ogg_opus_native_get_decoder_channels.argtypes = [c_void_p]

        self._lib.ogg_opus_native_is_eos.restype = c_int
        self._lib.ogg_opus_native_is_eos.argtypes = [c_void_p]

        self._ctx = self._lib.ogg_opus_native_create()
        if not self._ctx:
            raise OggOpusNativeError("创建 ogg_opus_native 上下文失败")

        self._closed = False

    def _load_library(self, lib_path: str | None):
        """
        默认优先查找：
        1. 传入的 lib_path
        2. 本文件目录下的 libogg_opus_native.so
        """
        candidates = []

        if lib_path:
            candidates.append(lib_path)

        current_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(current_dir, "libogg_opus_native.so"))

        last_err = None
        for path in candidates:
            if not path:
                continue
            if not os.path.exists(path):
                continue
            try:
                return ctypes.CDLL(path)
            except OSError as e:
                last_err = e

        raise OggOpusNativeError(
            f"无法加载 libogg_opus_native.so，候选路径: {candidates}, last_err={last_err}"
        )

    def _ensure_open(self):
        if self._closed or not self._ctx:
            raise OggOpusNativeError("OggOpusNative 已关闭")

    def _raise_last_error(self, default_msg: str):
        msg = default_msg
        try:
            raw = self._lib.ogg_opus_native_get_last_error(self._ctx)
            if raw:
                msg = raw.decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise OggOpusNativeError(msg)

    def feed(self, data: bytes) -> tuple[bytes, int]:
        """
        喂入一段 OGG-Opus 字节流，返回：
        (pcm_bytes, channels)

        pcm_bytes:
          48kHz, little-endian, int16 PCM
        channels:
          1 或 2；如果当前还没解出有效音频，可能为 0
        """
        self._ensure_open()

        if not data:
            return b"", self.channels

        buf = (c_ubyte * len(data)).from_buffer_copy(data)
        ret = self._lib.ogg_opus_native_feed(self._ctx, buf, len(data))
        if ret != 0:
            self._raise_last_error("ogg_opus_native_feed 失败")

        pcm = self._read_output_bytes()
        ch = self.channels
        self._lib.ogg_opus_native_clear_output(self._ctx)
        return pcm, ch

    def flush(self) -> tuple[bytes, int]:
        """
        在 audio.done 时调用，冲刷当前还能取出的尾部 PCM。
        """
        self._ensure_open()

        ret = self._lib.ogg_opus_native_flush(self._ctx)
        if ret != 0:
            self._raise_last_error("ogg_opus_native_flush 失败")

        pcm = self._read_output_bytes()
        ch = self.channels
        self._lib.ogg_opus_native_clear_output(self._ctx)
        return pcm, ch

    def reset(self):
        self._ensure_open()
        self._lib.ogg_opus_native_reset(self._ctx)

    def close(self):
        if self._closed:
            return
        if self._ctx:
            self._lib.ogg_opus_native_destroy(self._ctx)
            self._ctx = None
        self._closed = True

    def _read_output_bytes(self) -> bytes:
        pcm_len = self._lib.ogg_opus_native_get_pcm_len(self._ctx)
        if pcm_len == 0:
            return b""

        pcm_ptr = self._lib.ogg_opus_native_get_pcm_ptr(self._ctx)
        if not pcm_ptr:
            return b""

        return ctypes.string_at(pcm_ptr, pcm_len)

    @property
    def channels(self) -> int:
        self._ensure_open()
        return int(self._lib.ogg_opus_native_get_decoder_channels(self._ctx))

    @property
    def eos(self) -> bool:
        self._ensure_open()
        return bool(self._lib.ogg_opus_native_is_eos(self._ctx))

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass