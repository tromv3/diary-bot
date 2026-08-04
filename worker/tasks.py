import asyncio
import logging
from typing import Any

from arq.connections import RedisSettings
from faster_whisper import WhisperModel

from core.config import settings

logger = logging.getLogger(__name__)


def _transcribe_sync(model: WhisperModel, audio_path: str) -> str:
    segments, _info = model.transcribe(audio_path, language="ru")
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_audio(ctx: dict[str, Any], audio_path: str) -> str:
    """Transcribe an audio file and return the text only.

    Deliberately thin: no DB writes, no quota, no Telegram reply. Every voice
    message now goes through a transcribe-then-confirm flow owned entirely by
    the bot process (FSM state lives there) — this job is a pure function so
    the worker never needs to touch FSM/RedisStorage.
    """
    model: WhisperModel = ctx["whisper_model"]
    return await asyncio.to_thread(_transcribe_sync, model, audio_path)


async def startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(level=logging.INFO)
    ctx["whisper_model"] = WhisperModel(
        settings.whisper_model_size, device="cuda", compute_type="float16"
    )
    logger.info("Worker ready: whisper model '%s' loaded on cuda", settings.whisper_model_size)


class WorkerSettings:
    functions = [transcribe_audio]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = 300
