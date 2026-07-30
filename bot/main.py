import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from core.config import settings
from core.constants import DAILY_WORD_LIMIT
from core.db import async_session
from core.llm import extract_theses
from core.models import Message as DiaryMessage
from core.models import Thesis
from core.quota import try_reserve_words

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот-дневник. Пока что я просто повторяю за тобой — "
        "скоро научусь сохранять и анализировать твои мысли."
    )


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
