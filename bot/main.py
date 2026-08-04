import asyncio
import datetime
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import func, select

from core.config import settings
from core.constants import (
    ANSWER_TYPE_NUMBER,
    DAILY_WORD_LIMIT,
    NEW_DIARY_BUTTON_LABEL,
    PHOTO_STORY_DEBOUNCE_SECONDS,
    QUESTIONS_POLL_INTERVAL_SECONDS,
    QUESTIONS_TIME,
    REPORT_BUTTON_LABEL,
    REPORT_PERIOD_DAYS,
    REPORT_PERIOD_LABELS,
    SOURCE_TEXT,
    SOURCE_VOICE,
    VOICE_JOB_TIMEOUT_SECONDS,
)
from core.db import async_session
from core.diaries import (
    DiaryButtonFilter,
    DiaryCreation,
    build_diary_keyboard,
    create_diary,
    ensure_active_diary,
    get_pinned_message_id,
    list_diaries,
    set_active_diary,
    set_pinned_message_id,
)
from core.llm import extract_theses, summarize_theses
from core.media import photo_path, voice_path
from core.models import Diary
from core.models import Message as DiaryMessage
from core.models import Photo, Thesis, UsageDaily, UserSettings
from core.photos import PhotoStory, link_pending_photos, peek_pending_photo_batch
from core.quota import (
    REQUEST_WORDS_CALLBACK,
    build_request_words_keyboard,
    grant_bonus_words,
    quota_exceeded_message,
    try_reserve_words,
)
from core.questions import (
    PendingQuestion,
    QuestionFlow,
    get_due_questions,
    get_questions_enabled,
    mark_question_asked,
    parse_number,
    peek_pending_question,
    set_questions_enabled,
)
from core.voice import VoiceConfirm, peek_pending_voice_confirm

router = Router()
redis_pool: ArqRedis | None = None
fsm_storage: RedisStorage | None = None

# In-memory only: which user has a debounce task pending after their most recent
# photo(s), when the current batch started (for the eventual `since` filter in
# link_pending_photos), the first caption seen in the batch (if any), and the
# diary that was active when the batch started. Lost on restart, which is an
# acceptable edge case for a single-instance personal bot.
_photo_batches: dict[int, tuple[datetime.datetime, asyncio.Task, str | None, int]] = {}

# In-memory only, same trade-off as _photo_batches: caps one admin notification
# per user per day when they tap "request more words" repeatedly after hitting
# the daily limit, without needing a DB column for something this disposable.
_word_request_dates: dict[int, datetime.date] = {}


async def _pin_active_diary(bot: Bot, chat_id: int, user_id: int, diary_name: str) -> None:
    """(Re-)pin a status message announcing the active diary.

    Sends a fresh message rather than editing the old one in place — Telegram
    private chats let a bot pin without admin rights, and always sending a new
    message sidesteps having to special-case "message is not modified" errors.
    The previous pin (if any) is unpinned; failures there are swallowed since
    it may already be gone (deleted/unpinned by the user).
    """
    async with async_session() as session:
        old_pinned_id = await get_pinned_message_id(session, user_id)

    sent = await bot.send_message(chat_id, f"📌 Активный дневник: {diary_name}")
    try:
        await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except TelegramBadRequest:
        logging.exception("Failed to pin active-diary message for user=%s", user_id)

    if old_pinned_id is not None:
        try:
            await bot.unpin_chat_message(chat_id, message_id=old_pinned_id)
        except TelegramBadRequest:
            pass

    async with async_session() as session:
        await set_pinned_message_id(session, user_id, sent.message_id)
        await session.commit()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    async with async_session() as session:
        diary = await ensure_active_diary(session, message.from_user.id)
        await session.commit()
        diaries = await list_diaries(session, message.from_user.id)
        pinned_id = await get_pinned_message_id(session, message.from_user.id)

    await message.answer(
        "Привет! Я бот-дневник. Пишешь мне текстом, голосом или фото — "
        "я сохраняю и выделяю тезисы. Дневник для записи выбирается кнопками внизу.",
        reply_markup=build_diary_keyboard(diaries),
    )
    if pinned_id is None:
        await _pin_active_diary(message.bot, message.chat.id, message.from_user.id, diary.name)


@router.message(DiaryCreation.waiting_name)
async def handle_new_diary_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or name == NEW_DIARY_BUTTON_LABEL:
        await message.answer("Название не может быть пустым. Напиши название дневника:")
        return

    async with async_session() as session:
        diary = await create_diary(session, message.from_user.id, name)
        await session.commit()
        diaries = await list_diaries(session, message.from_user.id)

    await state.clear()
    await message.answer(
        f"Дневник «{name}» создан и выбран.", reply_markup=build_diary_keyboard(diaries)
    )
    await _pin_active_diary(message.bot, message.chat.id, message.from_user.id, diary.name)


@router.message(F.text == NEW_DIARY_BUTTON_LABEL)
async def handle_new_diary_button(message: Message, state: FSMContext) -> None:
    await state.set_state(DiaryCreation.waiting_name)
    await message.answer("Как назвать новый дневник?")


@router.message(F.text, DiaryButtonFilter())
async def handle_diary_switch(message: Message, matched_diary: Diary) -> None:
    async with async_session() as session:
        await set_active_diary(session, message.from_user.id, matched_diary.id)
        await session.commit()
        diaries = await list_diaries(session, message.from_user.id)

    await message.answer(
        f"Дневник переключён: {matched_diary.name}", reply_markup=build_diary_keyboard(diaries)
    )
    await _pin_active_diary(message.bot, message.chat.id, message.from_user.id, matched_diary.name)


def _report_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"report:{period}")
                for period, label in REPORT_PERIOD_LABELS.items()
            ]
        ]
    )


async def _generate_report(user_id: int, period: str) -> str:
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=REPORT_PERIOD_DAYS[period] - 1)
    range_end = today + datetime.timedelta(days=1)  # exclusive, start of tomorrow

    async with async_session() as session:
        diary = await ensure_active_diary(session, user_id)
        await session.commit()
        theses = (
            await session.execute(
                select(Thesis)
                .where(
                    Thesis.diary_id == diary.id,
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

    lines = [f"Отчёт «{diary.name}» за {period_label}", words_line, ""]
    if summary:
        lines.append("Тезисы:")
        lines.extend(f"- {t}" for t in summary)
    else:
        lines.append("Тезисов пока нет.")

    return "\n".join(lines)


@router.message(Command("report"))
async def handle_report(message: Message, command: CommandObject) -> None:
    period = (command.args or "day").strip().lower()
    if period not in REPORT_PERIOD_DAYS:
        await message.answer("Использование: /report [day|week|month]")
        return

    progress = await message.answer("Готовлю отчёт...")
    text = await _generate_report(message.from_user.id, period)
    await progress.edit_text(text)


@router.message(F.text == REPORT_BUTTON_LABEL)
async def handle_report_button(message: Message) -> None:
    await message.answer("За какой период?", reply_markup=_report_period_keyboard())


@router.callback_query(F.data.startswith("report:"))
async def handle_report_period_callback(callback: CallbackQuery) -> None:
    period = callback.data.split(":", 1)[1]
    # Ack the tap and swap in a placeholder immediately — summarize_theses is an
    # LLM call and can take a few seconds, during which the button would otherwise
    # look unresponsive with no feedback that anything happened.
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text("Готовлю отчёт...", reply_markup=None)
    text = await _generate_report(callback.from_user.id, period)
    if callback.message is not None:
        await callback.message.edit_text(text)


@router.callback_query(F.data == REQUEST_WORDS_CALLBACK)
async def handle_request_words(callback: CallbackQuery) -> None:
    user = callback.from_user
    today = datetime.date.today()
    if _word_request_dates.get(user.id) == today:
        await callback.answer(
            "Запрос уже отправлен сегодня, дождитесь ответа администратора.", show_alert=True
        )
        return

    _word_request_dates[user.id] = today
    who = f"@{user.username}" if user.username else user.full_name
    notice = f"{who} (id {user.id}) запросил больше слов.\n/grant {user.id} <кол-во>"
    for admin_id in settings.admin_ids:
        try:
            await callback.bot.send_message(admin_id, notice)
        except Exception:
            logging.exception("Failed to notify admin %s about word request from %s", admin_id, user.id)

    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Запрос отправлен администратору.")


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


@router.message(Command("questions"))
async def handle_questions_toggle(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    async with async_session() as session:
        await ensure_active_diary(session, message.from_user.id)  # guarantees a settings row exists
        await session.commit()
        if arg in ("on", "off"):
            await set_questions_enabled(session, message.from_user.id, arg == "on")
            await session.commit()
        enabled = await get_questions_enabled(session, message.from_user.id)

    status = "включены" if enabled else "выключены"
    await message.answer(f"Проактивные вопросы сейчас {status}.\nИспользование: /questions on|off")


async def _save_diary_entry(
    user_id: int,
    chat_id: int,
    bot: Bot,
    text: str,
    diary_id: int,
    linked_since: datetime.datetime | None,
    source: str = SOURCE_TEXT,
    audio_path: str | None = None,
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
                chat_id,
                quota_exceeded_message(f"Дневной лимит {DAILY_WORD_LIMIT} слов исчерпан."),
                reply_markup=build_request_words_keyboard(),
            )
            return None

        diary = await session.get(Diary, diary_id)
        diary_message = DiaryMessage(
            user_id=user_id,
            diary_id=diary_id,
            text=text,
            word_count=word_count,
            source=source,
            audio_path=audio_path,
        )
        session.add(diary_message)
        await session.commit()

        linked_photos = 0
        if linked_since is not None:
            linked_photos = await link_pending_photos(
                session, user_id, diary_id, linked_since, diary_message.id
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
                Thesis(message_id=diary_message.id, user_id=user_id, diary_id=diary_id, text=t)
                for t in theses
            )
            await session.commit()

    reply = f"[{diary.name}] Сохранил ({word_count} слов"
    reply += f", фото: {linked_photos})." if linked_photos else ")."
    if theses:
        reply += "\n\nТезисы:\n" + "\n".join(f"- {t}" for t in theses)
    await bot.send_message(chat_id, reply)
    return diary_message


def _fsm_context(bot_id: int, user_id: int) -> FSMContext:
    """Build an FSMContext outside of any incoming update.

    Needed by the question scheduler (no update to hand a context over) and by
    the voice-transcript background task (running detached from the handler
    that spawned it). Assumes chat_id == user_id, true for this private-chat
    personal bot — the same assumption the scheduler already makes.
    """
    assert fsm_storage is not None
    return FSMContext(storage=fsm_storage, key=StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id))


def _voice_confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Верно", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="✏️ Исправить", callback_data=f"{prefix}:no"),
            ]
        ]
    )


async def _send_next_question(
    bot: Bot, user_id: int, chat_id: int, state: FSMContext, diary_id: int, queue: list[dict]
) -> None:
    question = queue[0]
    async with async_session() as session:
        await mark_question_asked(session, user_id, question["key"])
        await session.commit()

    await state.set_state(QuestionFlow.waiting)
    await state.update_data(
        queue=queue,
        diary_id=diary_id,
        asked_at=datetime.datetime.now().isoformat(),
        pending_voice_text=None,
    )
    await bot.send_message(chat_id, question["text"])


async def _accept_question_answer(
    bot: Bot,
    user_id: int,
    chat_id: int,
    state: FSMContext,
    pending: PendingQuestion,
    raw_text: str,
) -> None:
    raw_text = raw_text.strip()
    if pending.answer_type == ANSWER_TYPE_NUMBER and parse_number(raw_text) is None:
        await state.update_data(pending_voice_text=None, asked_at=datetime.datetime.now().isoformat())
        await bot.send_message(
            chat_id, f"Не понял число, пришли, пожалуйста, просто число.\n{pending.text}"
        )
        return  # validation retry — does not advance the queue, is not a timeout/skip
    if not raw_text:
        await state.update_data(pending_voice_text=None, asked_at=datetime.datetime.now().isoformat())
        await bot.send_message(chat_id, f"Пустой ответ не принимается.\n{pending.text}")
        return

    text = f"{pending.text} {raw_text}"
    diary_message = await _save_diary_entry(user_id, chat_id, bot, text, pending.diary_id, None)
    if diary_message is None:
        # Quota exceeded — _save_diary_entry already sent that message. Leave the
        # FSM state as-is so a later reply (e.g. after requesting more words)
        # retries the same question, same as handle_echo does for photo stories.
        return

    data = await state.get_data()
    remaining = data["queue"][1:]
    if remaining:
        await _send_next_question(bot, user_id, chat_id, state, pending.diary_id, remaining)
    else:
        await state.clear()
        await bot.send_message(chat_id, "Спасибо! На сегодня вопросы закончились 🙂")


async def _await_question_voice_transcript(job, bot: Bot, user_id: int, chat_id: int) -> None:
    try:
        text = (await job.result(timeout=VOICE_JOB_TIMEOUT_SECONDS) or "").strip()
    except Exception:
        logging.exception("Question-answer transcription failed for user=%s", user_id)
        await bot.send_message(chat_id, "Не получилось распознать голос, попробуй ещё раз.")
        return
    if not text:
        await bot.send_message(chat_id, "Не удалось распознать речь, попробуй ещё раз текстом или голосом.")
        return

    state = _fsm_context(bot.id, user_id)
    pending = await peek_pending_question(state)
    if pending is None:
        return  # window already expired or queue moved on meanwhile

    await state.update_data(pending_voice_text=text, asked_at=datetime.datetime.now().isoformat())
    await bot.send_message(
        chat_id, f"Понял: «{text}»\nВсё верно?", reply_markup=_voice_confirm_keyboard("qvoice")
    )


async def _await_voice_transcript(
    job, bot: Bot, user_id: int, chat_id: int, state: FSMContext
) -> None:
    try:
        text = (await job.result(timeout=VOICE_JOB_TIMEOUT_SECONDS) or "").strip()
    except Exception:
        logging.exception("Voice transcription failed for user=%s", user_id)
        await state.clear()
        await bot.send_message(chat_id, "Не получилось распознать голос, попробуй ещё раз.")
        return
    if not text:
        await state.clear()
        await bot.send_message(chat_id, "Не удалось распознать речь, попробуй ещё раз текстом или голосом.")
        return

    pending = await peek_pending_voice_confirm(state)
    if pending is None:
        return  # window already expired

    await state.update_data(transcript=text)
    await bot.send_message(
        chat_id, f"Понял: «{text}»\nВсё верно?", reply_markup=_voice_confirm_keyboard("vconfirm")
    )


@router.callback_query(F.data.in_({"qvoice:yes", "qvoice:no"}))
async def handle_question_voice_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pending = await peek_pending_question(state)
    if pending is None or pending.pending_voice_text is None:
        return

    user_id = callback.from_user.id
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)

    if callback.data == "qvoice:no":
        await state.update_data(pending_voice_text=None, asked_at=datetime.datetime.now().isoformat())
        await callback.bot.send_message(user_id, f"Хорошо, ответь ещё раз:\n{pending.text}")
        return

    await _accept_question_answer(
        callback.bot, user_id, user_id, state, pending, pending.pending_voice_text
    )


@router.callback_query(F.data.in_({"vconfirm:yes", "vconfirm:no"}))
async def handle_voice_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    pending = await peek_pending_voice_confirm(state)
    if pending is None or pending.transcript is None:
        return  # still transcribing, or the window already expired — ignore a stray tap

    user_id = callback.from_user.id
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)

    if callback.data == "vconfirm:no":
        await state.clear()
        await callback.bot.send_message(user_id, "Хорошо, отправь ещё раз текстом или голосом.")
        return

    data = await state.get_data()
    await state.clear()
    await _save_diary_entry(
        user_id,
        user_id,
        callback.bot,
        pending.transcript,
        pending.diary_id,
        pending.linked_since,
        source=SOURCE_VOICE,
        audio_path=data.get("audio_path"),
    )


async def _dispatch_due_questions(bot: Bot) -> None:
    now = datetime.datetime.now()
    if now.time() < QUESTIONS_TIME:
        return

    async with async_session() as session:
        user_ids = (
            await session.execute(
                select(UserSettings.user_id).where(UserSettings.questions_enabled.is_(True))
            )
        ).scalars().all()

    for user_id in user_ids:
        state = _fsm_context(bot.id, user_id)
        if await state.get_state() is not None:
            continue  # mid photo-story / diary-creation / already mid question-flow — retry next tick

        async with async_session() as session:
            due = await get_due_questions(session, user_id, now.date())
            if not due:
                continue
            diary = await ensure_active_diary(session, user_id)
            await session.commit()

        queue = [{"key": q.key, "text": q.text, "answer_type": q.answer_type} for q in due]
        await _send_next_question(bot, user_id, user_id, state, diary.id, queue)


async def _run_question_scheduler(bot: Bot) -> None:
    while True:
        try:
            await _dispatch_due_questions(bot)
        except Exception:
            logging.exception("Question scheduler tick failed")
        await asyncio.sleep(QUESTIONS_POLL_INTERVAL_SECONDS)


async def _resolve_photo_batch(
    user_id: int, chat_id: int, bot: Bot, state: FSMContext
) -> None:
    await asyncio.sleep(PHOTO_STORY_DEBOUNCE_SECONDS)
    batch_start, _, caption, diary_id = _photo_batches.pop(user_id, (None, None, None, None))
    if batch_start is None:
        return

    if caption:
        # A caption already tells the story — save it right away instead of
        # asking, and skip the FSM wait entirely.
        await _save_diary_entry(user_id, chat_id, bot, caption, diary_id, batch_start)
        return

    await state.set_state(PhotoStory.waiting)
    await state.update_data(batch_start=batch_start.isoformat(), diary_id=diary_id)
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

    pending_story = await peek_pending_photo_batch(state)
    if pending_story is not None:
        # A question is already pending for an earlier (or this) batch — this
        # photo joins it via the same diary + the `created_at >= since` filter
        # once the story arrives, no need to ask again.
        async with async_session() as session:
            session.add(
                Photo(
                    user_id=user_id,
                    diary_id=pending_story.diary_id,
                    media_group_id=message.media_group_id,
                    file_path=str(path),
                )
            )
            await session.commit()
        return

    pending = _photo_batches.get(user_id)
    if pending is not None:
        batch_start, task, caption, diary_id = pending
        task.cancel()
        # Only the first caption seen in the batch counts (Telegram albums put
        # the caption on a single photo anyway).
        caption = caption or message.caption
    else:
        batch_start = received_at
        caption = message.caption
        async with async_session() as session:
            diary = await ensure_active_diary(session, user_id)
            await session.commit()
            diary_id = diary.id

    async with async_session() as session:
        session.add(
            Photo(
                user_id=user_id,
                diary_id=diary_id,
                media_group_id=message.media_group_id,
                file_path=str(path),
            )
        )
        await session.commit()

    task = asyncio.create_task(
        _resolve_photo_batch(user_id, message.chat.id, message.bot, state)
    )
    _photo_batches[user_id] = (batch_start, task, caption, diary_id)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    voice = message.voice
    path = voice_path(user_id, voice.file_unique_id)
    await message.bot.download(voice, destination=path)

    pending_question = await peek_pending_question(state)
    if pending_question is not None:
        job = await redis_pool.enqueue_job("transcribe_audio", str(path))
        asyncio.create_task(
            _await_question_voice_transcript(job, message.bot, user_id, message.chat.id)
        )
        await message.answer("Секунду, слушаю голосовое...")
        return

    pending = await peek_pending_photo_batch(state)
    if pending is not None:
        diary_id, since = pending.diary_id, pending.since
    else:
        async with async_session() as session:
            diary = await ensure_active_diary(session, user_id)
            await session.commit()
            diary_id = diary.id
        since = None

    # Claim the FSM slot up front (before transcription finishes) so a second
    # voice/photo arriving mid-transcription doesn't race into the same pending
    # photo-story. This also supersedes any pending photo-story state — same
    # trade-off it already had (can't cheaply defer consuming it across the
    # async transcription gap), just via a new intermediate state instead of
    # an immediate state.clear().
    await state.set_state(VoiceConfirm.waiting)
    await state.update_data(
        diary_id=diary_id,
        linked_since=since.isoformat() if since else None,
        audio_path=str(path),
        transcript=None,
        started_at=datetime.datetime.now().isoformat(),
    )
    job = await redis_pool.enqueue_job("transcribe_audio", str(path))
    asyncio.create_task(_await_voice_transcript(job, message.bot, user_id, message.chat.id, state))
    await message.answer("Секунду, слушаю голосовое...")


@router.message(F.text)
async def handle_echo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    pending_question = await peek_pending_question(state)
    if pending_question is not None:
        await _accept_question_answer(
            message.bot, user_id, message.chat.id, state, pending_question, message.text
        )
        return

    pending = await peek_pending_photo_batch(state)
    if pending is not None:
        diary_id, since = pending.diary_id, pending.since
    else:
        async with async_session() as session:
            diary = await ensure_active_diary(session, user_id)
            await session.commit()
            diary_id = diary.id
        since = None

    diary_message = await _save_diary_entry(
        user_id, message.chat.id, message.bot, message.text, diary_id, since
    )
    # Only consume the pending-photo state once the story actually saved —
    # a quota rejection shouldn't burn the user's answer window.
    if diary_message is not None and pending is not None:
        await state.clear()


async def main() -> None:
    global redis_pool, fsm_storage
    logging.basicConfig(level=logging.INFO)
    redis_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    bot = Bot(token=settings.bot_token)
    fsm_storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=fsm_storage)
    dp.include_router(router)

    scheduler_task = asyncio.create_task(_run_question_scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
