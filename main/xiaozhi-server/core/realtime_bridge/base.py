class BaseRealtimeClient:
    async def connect(self, conn):
        raise NotImplementedError

    async def send_audio(self, conn, provider, audio_bytes: bytes):
        raise NotImplementedError

    async def send_text(self, conn, provider, text: str):
        raise NotImplementedError

    async def commit_input(self, conn, provider):
        raise NotImplementedError

    async def recv_event(self, conn, provider):
        raise NotImplementedError

    async def close(self, conn, provider):
        raise NotImplementedError