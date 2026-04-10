from .doubao.client import DoubaoRealtimeClient
# from .chatgpt.client import ChatGPTRealtimeClient


def create_provider_client(config: dict):
    config = config or {}
    provider = config.get("provider", "doubao")

    if provider == "doubao":
        return DoubaoRealtimeClient(config)

    # if provider == "chatgpt":
    #     return ChatGPTRealtimeClient(config)

    raise ValueError(f"unsupported realtime provider: {provider}")