from typing import Any

from aiogram.filters import BaseFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import CATEGORY_GENERAL, DEFAULT_DIARY_NAME, NEW_DIARY_BUTTON_LABEL
from core.db import async_session
from core.models import Diary, UserSettings


class DiaryCreation(StatesGroup):
    waiting_name = State()


async def list_diaries(session: AsyncSession, user_id: int) -> list[Diary]:
    return (
        (
            await session.execute(
                select(Diary).where(Diary.user_id == user_id).order_by(Diary.id)
            )
        )
        .scalars()
        .all()
    )


async def ensure_active_diary(session: AsyncSession, user_id: int) -> Diary:
    """Return the user's active diary, creating a default one on first use.

    Race-safe the same way as quota.try_reserve_words: INSERT ... ON CONFLICT
    DO NOTHING + re-read. If two concurrent calls both lose the pre-check, one
    of them just leaves behind an unused "Основной" diary — harmless for a
    single-user-per-account personal bot, not worth cleaning up.
    """
    settings_row = await session.get(UserSettings, user_id)
    if settings_row is not None:
        return await session.get(Diary, settings_row.active_diary_id)

    diary = Diary(user_id=user_id, name=DEFAULT_DIARY_NAME, category=CATEGORY_GENERAL)
    session.add(diary)
    await session.flush()

    insert_settings = (
        pg_insert(UserSettings)
        .values(user_id=user_id, active_diary_id=diary.id)
        .on_conflict_do_nothing()
    )
    await session.execute(insert_settings)

    settings_row = await session.get(UserSettings, user_id)
    return await session.get(Diary, settings_row.active_diary_id)


async def set_active_diary(session: AsyncSession, user_id: int, diary_id: int) -> None:
    await session.execute(
        update(UserSettings)
        .where(UserSettings.user_id == user_id)
        .values(active_diary_id=diary_id)
    )


async def create_diary(session: AsyncSession, user_id: int, name: str) -> Diary:
    diary = Diary(user_id=user_id, name=name, category=CATEGORY_GENERAL)
    session.add(diary)
    await session.flush()
    await set_active_diary(session, user_id, diary.id)
    return diary


def build_diary_keyboard(diaries: list[Diary]) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=diary.name)] for diary in diaries]
    rows.append([KeyboardButton(text=NEW_DIARY_BUTTON_LABEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class DiaryButtonFilter(BaseFilter):
    """Matches a tap on one of the user's diary-switch keyboard buttons."""

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        if not message.text:
            return False
        async with async_session() as session:
            diaries = await list_diaries(session, message.from_user.id)
        for diary in diaries:
            if message.text == diary.name:
                return {"matched_diary": diary}
        return False
