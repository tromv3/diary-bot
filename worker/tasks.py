import asyncio
import datetime
import logging
from typing import Any

from aiogram import Bot
from arq.connections import RedisSettings
from faster_whisper import WhisperModel

from core.config import settings
from core.constants import DAILY_WORD_LIMIT, SOURCE_VOICE
from core.db import async_session
from core.llm import extract_theses
from core.models import Diary
from core.models import Message as DiaryMessage
from core.models import Thesis
from core.photos import link_pending_photos
from core.quota import build_request_words_keyboard, quota_exceeded_message, try_reserve_words

logger = logging.getLogger(__name__)


def _transcribe_sync(model: WhisperModel, audio_path: str) -> str:
    segments, _info = model.transcribe(audio_path, language="ru")
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe_voice(
    ctx: dict[str, Any],
    audio_path: str,
    user_id: int,
    chat_id: int,
    diary_id: int,
    photo_batch_since: str | None = None,
) -> None:
    bot: Bot = ctx["bot"]
    model: WhisperModel = ctx["whisper_model"]

    text = await asyncio.to_thread(_transcribe_sync, model, audio_path)
    if not text:
        await bot.send_message(chat_id, "Не удалось распознать речь в голосовом сообщении.")
        return

    word_count = len(text.split())
    async with async_session() as session:
        allowed = await try_reserve_words(session, user_id, word_count)
        if not allowed:
            await session.commit()
            await bot.send_message(
                chat_id,
                quota_exceeded_message(
                    f"Распознал голосовое ({word_count} слов), но дневной лимит "
                    f"{DAILY_WORD_LIMIT} слов уже исчерпан — сообщение не сохранено."
                ),
                reply_markup=build_request_words_keyboard(),
            )
            return

        diary = await session.get(Diary, diary_id)
        diary_message = DiaryMessage(
            user_id=user_id,
            diary_id=diary_id,
            text=text,
            word_count=word_count,
            source=SOURCE_VOICE,
            audio_path=audio_path,
        )
        session.add(diary_message)
        await session.commit()

        linked_photos = 0
        if photo_batch_since is not None:
            linked_photos = await link_pending_photos(
                session,
                user_id,
                diary_id,
                datetime.datetime.fromisoformat(photo_batch_since),
                diary_message.id,
            )
            await session.commit()

    theses: list[str] = []
    try:
        # Same reasoning as in the bot's text handler: an extraction failure
        # must not lose a transcript that's already saved.
        theses = await extract_theses(text)
    except Exception:
        logger.exception("Failed to extract theses for voice message %s", diary_message.id)

    if theses:
        async with async_session() as session:
            session.add_all(
                Thesis(message_id=diary_message.id, user_id=user_id, diary_id=diary_id, text=t)
                for t in theses
            )
            await session.commit()

    reply = f"[{diary.name}] Распознал голосовое ({word_count} слов"
    reply += f", фото: {linked_photos})" if linked_photos else ")"
    reply += f":\n\n{text}"
    if theses:
        reply += "\n\nТезисы:\n" + "\n".join(f"- {t}" for t in theses)
    await bot.send_message(chat_id, reply)


async def startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(level=logging.INFO)
    ctx["bot"] = Bot(token=settings.bot_token)
    ctx["whisper_model"] = WhisperModel(
        settings.whisper_model_size, device="cuda", compute_type="float16"
    )
    logger.info("Worker ready: whisper model '%s' loaded on cuda", settings.whisper_model_size)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["bot"].session.close()


class WorkerSettings:
    functions = [transcribe_voice]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = 300
