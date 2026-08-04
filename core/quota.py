import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.constants import DAILY_WORD_LIMIT
from core.models import UsageDaily, UserCredit

REQUEST_WORDS_CALLBACK = "request_words"


async def try_reserve_words(session: AsyncSession, user_id: int, word_count: int) -> bool:
    today = datetime.date.today()

    ensure_usage_row = (
        pg_insert(UsageDaily)
        .values(user_id=user_id, date=today, words_used=0)
        .on_conflict_do_nothing()
    )
    await session.execute(ensure_usage_row)

    usage = (
        await session.execute(
            select(UsageDaily)
            .where(UsageDaily.user_id == user_id, UsageDaily.date == today)
            .with_for_update()
        )
    ).scalar_one()

    remaining_free = max(DAILY_WORD_LIMIT - usage.words_used, 0)
    if word_count <= remaining_free:
        usage.words_used += word_count
        return True

    # Daily free quota isn't enough on its own — draw the rest from the
    # user's purchased/granted bonus balance, all-or-nothing.
    needed_from_bonus = word_count - remaining_free

    ensure_credit_row = (
        pg_insert(UserCredit)
        .values(user_id=user_id, bonus_words=0)
        .on_conflict_do_nothing()
    )
    await session.execute(ensure_credit_row)

    credit = (
        await session.execute(
            select(UserCredit).where(UserCredit.user_id == user_id).with_for_update()
        )
    ).scalar_one()

    if credit.bonus_words < needed_from_bonus:
        return False

    usage.words_used = DAILY_WORD_LIMIT
    credit.bonus_words -= needed_from_bonus
    return True


async def grant_bonus_words(session: AsyncSession, user_id: int, words: int) -> int:
    """Add to a user's standing bonus balance, returning the new total."""
    stmt = (
        pg_insert(UserCredit)
        .values(user_id=user_id, bonus_words=words)
        .on_conflict_do_update(
            index_elements=[UserCredit.user_id],
            set_={"bonus_words": UserCredit.bonus_words + words},
        )
        .returning(UserCredit.bonus_words)
    )
    return (await session.execute(stmt)).scalar_one()


def quota_exceeded_message(prefix: str) -> str:
    text = f"{prefix} Нажмите кнопку ниже, чтобы запросить больше слов у администратора"
    if settings.admin_contact:
        text += f", либо напишите {settings.admin_contact} напрямую"
    return text + "."


def build_request_words_keyboard() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(
        text="Запросить больше слов", callback_data=REQUEST_WORDS_CALLBACK
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
