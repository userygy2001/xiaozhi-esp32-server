from core.utils.util import audio_bytes_to_data_stream, pcm_to_data_stream


def pcm_to_xiaozhi_opus_frames(raw_pcm: bytes, sample_rate=16000):
    frames = []

    pcm_to_data_stream(
        raw_pcm,
        is_opus=True,
        callback=lambda frame: frames.append(frame),
        sample_rate=sample_rate,
    )
    return frames


def audio_bytes_to_xiaozhi_frames(audio_bytes: bytes, file_type: str, sample_rate=16000):
    frames = []

    audio_bytes_to_data_stream(
        audio_bytes,
        file_type=file_type,
        is_opus=True,
        callback=lambda frame: frames.append(frame),
        sample_rate=sample_rate,
    )
    return frames