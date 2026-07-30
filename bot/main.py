import asyncio
import datetime
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select

from core.config import settings
from core.constants import DAILY_WORD_LIMIT
from core.db import async_session
from core.llm import extract_theses, summarize_theses
from core.models import Message as DiaryMessage
from core.models import Thesis, UsageDaily
from core.quota import try_reserve_words

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот-дневник. Пока что я просто повторяю за тобой — "
        "скоро научусь сохранять и анализировать твои мысли."
    )


@router.message(Command("report"))
async def handle_report(message: Message) -> None:
    user_id = message.from_user.id
    today = datetime.date.today()

    async with async_session() as session:
        theses = (
            await session.execute(
                select(Thesis)
                .where(Thesis.user_id == user_id, func.date(Thesis.created_at) == today)
                .order_by(Thesis.created_at)
            )
        ).scalars().all()
        usage = await session.get(UsageDaily, (user_id, today))

    thesis_texts = [t.text for t in theses]
    summary = thesis_texts
    if thesis_texts:
        try:
            # Broad on purpose, same reasoning as in handle_echo: fall back to the
            # raw (possibly repetitive) list rather than fail the whole report.
            summary = await summarize_theses(thesis_texts)
        except Exception:
            logging.exception(
                "Failed to summarize theses for report user=%s date=%s", user_id, today
            )

    words_used = usage.words_used if usage else 0
    lines = [
        f"Отчёт за {today.isoformat()}",
        f"Слов написано: {words_used} из {DAILY_WORD_LIMIT}",
        "",
    ]
    if summary:
        lines.append("Тезисы дня:")
        lines.extend(f"- {t}" for t in summary)
    else:
        lines.append("Тезисов пока нет.")

    await message.answer("\n".join(lines))


@router.message(F.text)
async def handle_echo(message: Message) -> None:
    word_count = len(message.text.split())
    async with async_session() as session:
        allowed = await try_reserve_words(session, message.from_user.id, word_count)
        if not allowed:
            await session.commit()
            await message.answer(
                f"Дневной лимит {DAILY_WORD_LIMIT} слов исчерпан. "
                "Напишите администратору, чтобы увеличить лимит."
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
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
