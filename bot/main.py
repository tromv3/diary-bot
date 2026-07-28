import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from core.config import settings
from core.db import async_session
from core.models import Message as DiaryMessage

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
        session.add(
            DiaryMessage(
                user_id=message.from_user.id,
                text=message.text,
                word_count=word_count,
            )
        )
        await session.commit()

    await message.answer(f"Сохранил ({word_count} слов).")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
