import asyncio
import datetime
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import func, select

from core.config import settings
from core.constants import DAILY_WORD_LIMIT, REPORT_PERIOD_DAYS
from core.db import async_session
from core.llm import extract_theses, summarize_theses
from core.media import voice_path
from core.models import Message as DiaryMessage
from core.models import Thesis, UsageDaily
from core.quota import grant_bonus_words, quota_exceeded_message, try_reserve_words

router = Router()
redis_pool: ArqRedis | None = None


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


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    voice = message.voice
    path = voice_path(message.from_user.id, voice.file_unique_id)
    await message.bot.download(voice, destination=path)

    await redis_pool.enqueue_job(
        "transcribe_voice", str(path), message.from_user.id, message.chat.id
    )
    await message.answer("Принял голосовое, распознаю...")


@router.message(F.text)
async def handle_echo(message: Message) -> None:
    word_count = len(message.text.split())
    async with async_session() as session:
        allowed = await try_reserve_words(session, message.from_user.id, word_count)
        if not allowed:
            await session.commit()
            await message.answer(
                quota_exceeded_message(f"Дневной лимит {DAILY_WORD_LIMIT} слов исчерпан.")
            )
            return

        diary_message = DiaryMessage(
            user_id=message.from_user.id,
            text=message.text,
            word_count=word_count,
        )
        session.add(diary_message)
        await session.commit()

    theses: list[str] = []
    try:
        # Broad on purpose: an OpenRouter/network hiccup must not lose the message
        # that's already saved, or crash the bot for one user's request.
        theses = await extract_theses(message.text)
    except Exception:
        logging.exception("Failed to extract theses for message %s", diary_message.id)

    if theses:
        async with async_session() as session:
            session.add_all(
                Thesis(message_id=diary_message.id, user_id=message.from_user.id, text=t)
                for t in theses
            )
            await session.commit()

    reply = f"Сохранил ({word_count} слов)."
    if theses:
        reply += "\n\nТезисы:\n" + "\n".join(f"- {t}" for t in theses)
    await message.answer(reply)


async def main() -> None:
    global redis_pool
    logging.basicConfig(level=logging.INFO)
    redis_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
