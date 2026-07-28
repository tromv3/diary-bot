import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import DAILY_WORD_LIMIT
from core.models import UsageDaily


async def try_reserve_words(session: AsyncSession, user_id: int, word_count: int) -> bool:
    today = datetime.date.today()

    ensure_row = (
        pg_insert(UsageDaily)
        .values(user_id=user_id, date=today, words_used=0)
        .on_conflict_do_nothing()
    )
    await session.execute(ensure_row)

    result = await session.execute(
        select(UsageDaily)
        .where(UsageDaily.user_id == user_id, UsageDaily.date == today)
        .with_for_update()
    )
    usage = result.scalar_one()

    if usage.words_used + word_count > DAILY_WORD_LIMIT:
        return False

    usage.words_used += word_count
    return True
