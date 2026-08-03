import asyncio
import datetime
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import func, select

from core.config import settings
from core.constants import DAILY_WORD_LIMIT, PHOTO_STORY_DEBOUNCE_SECONDS, REPORT_PERIOD_DAYS
from core.db import async_session
from core.llm import extract_theses, summarize_theses
from core.media import photo_path, voice_path
from core.models import Message as DiaryMessage
from core.models import Photo, Thesis, UsageDaily
from core.photos import PhotoStory, link_pending_photos, peek_pending_photo_batch
from core.quota import grant_bonus_words, quota_exceeded_message, try_reserve_words

router = Router()
redis_pool: ArqRedis | None = None

# In-memory only: which user has a debounce task pending after their most recent
# photo(s), when the current batch started (for the eventual `since` filter in
# link_pending_photos), and the first caption seen in the batch, if any. Lost on
# restart, which is an acceptable edge case for a single-instance personal bot.
_photo_batches: dict[int, tuple[datetime.datetime, asyncio.Task, str | None]] = {}


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот-дневник. Пока что я просто повторяю за тобой — "
        "скоро научусь сохранять и анализировать твои мысли."
    )


@router.message(Command("report"))
async def handle_report(message: Message, command: CommandObject) -> None:
    period = (command.args or "day").strip().lower()
    if period not in REPORT_PERIOD_DAYS:
        await message.answer("Использование: /report [day|week|month]")
        return

    user_id = message.from_user.id
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=REPORT_PERIOD_DAYS[period] - 1)
    range_end = today + datetime.timedelta(days=1)  # exclusive, start of tomorrow

    async with async_session() as session:
        theses = (
            await session.execute(
                select(Thesis)
                .where(
                    Thesis.user_id == user_id,
                    Thesis.created_at >= start_date,
                    Thesis.created_at < range_end,
                )
                .order_by(Thesis.created_at)
            )
        ).scalars().all()
        words_used = await session.scalar(
            select(func.coalesce(func.sum(UsageDaily.words_used), 0)).where(
                UsageDaily.user_id == user_id,
                UsageDaily.date >= start_date,
                UsageDaily.date <= today,
            )
        )

    thesis_texts = [t.text for t in theses]
    summary = thesis_texts
    if thesis_texts:
        try:
            # Broad on purpose, same reasoning as in handle_echo: fall back to the
            # raw (possibly repetitive) list rather than fail the whole report.
            summary = await summarize_theses(thesis_texts)
        except Exception:
            logging.exception(
                "Failed to summarize theses for report user=%s period=%s", user_id, period
            )

    if period == "day":
        period_label = today.isoformat()
        words_line = f"Слов написано: {words_used} из {DAILY_WORD_LIMIT}"
    else:
        period_label = f"{start_date.isoformat()} — {today.isoformat()}"
        words_line = f"Слов написано: {words_used}"

    lines = [f"Отчёт за {period_label}", words_line, ""]
    if summary:
        lines.append("Тезисы:")
        lines.extend(f"- {t}" for t in summary)
    else:
        lines.append("Тезисов пока нет.")

    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def handle_grant(message: Message, command: CommandObject) -> None:
    if message.from_user.id not in settings.admin_ids:
        return

    parts = (command.args or "").split()
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        await message.answer("Использование: /grant <user_id> <слов>")
        return

    target_user_id, words = int(parts[0]), int(parts[1])
    if words <= 0:
        await message.answer("Количество слов должно быть положительным.")
        return

    async with async_session() as session:
        new_balance = await grant_bonus_words(session, target_user_id, words)
        await session.commit()

    await message.answer(
        f"Начислено {words} слов пользователю {target_user_id}. "
        f"Баланс сверх дневного лимита: {new_balance}."
    )


async def _save_diary_entry(
    user_id: int,
    chat_id: int,
    bot: Bot,
    text: str,
    linked_since: datetime.datetime | None,
) -> DiaryMessage | None:
    """Reserve quota, persist the message (+ any pending photos, + theses), reply.

    Returns the saved message, or None if the daily quota was exceeded (in which
    case nothing was persisted and the caller should not treat this as consumed).
    """
    word_count = len(text.split())
    async with async_session() as session:
        allowed = await try_reserve_words(session, user_id, word_count)
        if not allowed:
            await session.commit()
            await bot.send_message(
                chat_id, quota_exceeded_message(f"Дневной лимит {DAILY_WORD_LIMIT} слов исчерпан.")
            )
            return None

        diary_message = DiaryMessage(user_id=user_id, text=text, word_count=word_count)
        session.add(diary_message)
        await session.commit()

        linked_photos = 0
        if linked_since is not None:
            linked_photos = await link_pending_photos(
                session, user_id, linked_since, diary_message.id
            )
            await session.commit()

    theses: list[str] = []
    try:
        # Broad on purpose: an OpenRouter/network hiccup must not lose the message
        # that's already saved, or crash the bot for one user's request.
        theses = await extract_theses(text)
    except Exception:
        logging.exception("Failed to extract theses for message %s", diary_message.id)

    if theses:
        async with async_session() as session:
            session.add_all(
                Thesis(message_id=diary_message.id, user_id=user_id, text=t) for t in theses
            )
            await session.commit()

    reply = f"Сохранил ({word_count} слов"
    reply += f", фото: {linked_photos})." if linked_photos else ")."
    if theses:
        reply += "\n\nТезисы:\n" + "\n".join(f"- {t}" for t in theses)
    await bot.send_message(chat_id, reply)
    return diary_message


async def _resolve_photo_batch(
    user_id: int, chat_id: int, bot: Bot, state: FSMContext
) -> None:
    await asyncio.sleep(PHOTO_STORY_DEBOUNCE_SECONDS)
    batch_start, _, caption = _photo_batches.pop(user_id, (None, None, None))
    if batch_start is None:
        return

    if caption:
        # A caption already tells the story — save it right away instead of
        # asking, and skip the FSM wait entirely.
        await _save_diary_entry(user_id, chat_id, bot, caption, batch_start)
        return

    await state.set_state(PhotoStory.waiting)
    await state.update_data(batch_start=batch_start.isoformat())
    await bot.send_message(chat_id, "Расскажи, что здесь происходит?")


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    # Captured before the download/insert below (which is when the DB actually
    # timestamps the row via created_at=func.now()), so this is guaranteed to be
    # <= that timestamp — link_pending_photos filters on created_at >= since.
    received_at = datetime.datetime.now()
    photo = message.photo[-1]
    path = photo_path(user_id, photo.file_unique_id)
    await message.bot.download(photo, destination=path)

    async with async_session() as session:
        session.add(
            Photo(user_id=user_id, media_group_id=message.media_group_id, file_path=str(path))
        )
        await session.commit()

    if await state.get_state() == PhotoStory.waiting.state:
        # Already asked (this batch or an earlier one is still pending an answer) —
        # this photo will be picked up by the `created_at >= batch_start` filter
        # once the story arrives, no need to ask again.
        return

    pending = _photo_batches.get(user_id)
    if pending is not None:
        batch_start, task, caption = pending
        task.cancel()
        # Only the first caption seen in the batch counts (Telegram albums put
        # the caption on a single photo anyway).
        caption = caption or message.caption
    else:
        batch_start = received_at
        caption = message.caption

    task = asyncio.create_task(
        _resolve_photo_batch(user_id, message.chat.id, message.bot, state)
    )
    _photo_batches[user_id] = (batch_start, task, caption)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext) -> None:
    voice = message.voice
    path = voice_path(message.from_user.id, voice.file_unique_id)
    await message.bot.download(voice, destination=path)

    since = await peek_pending_photo_batch(state)
    if since is not None:
        # Can't cheaply defer consuming this until the worker actually saves the
        # message (different process) — accept the small edge case where a
        # failed transcription/quota check in the worker leaves the photos
        # unlinked, same as the general "no story" fallback.
        await state.clear()

    await redis_pool.enqueue_job(
        "transcribe_voice",
        str(path),
        message.from_user.id,
        message.chat.id,
        since.isoformat() if since else None,
    )
    await message.answer("Принял голосовое, распознаю...")


@router.message(F.text)
async def handle_echo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    since = await peek_pending_photo_batch(state)

    diary_message = await _save_diary_entry(
        user_id, message.chat.id, message.bot, message.text, since
    )
    # Only consume the pending-photo state once the story actually saved —
    # a quota rejection shouldn't burn the user's answer window.
    if diary_message is not None and since is not None:
        await state.clear()


async def main() -> None:
    global redis_pool
    logging.basicConfig(level=logging.INFO)
    redis_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    bot = Bot(token=settings.bot_token)
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
