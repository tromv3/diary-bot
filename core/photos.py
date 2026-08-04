import datetime
from dataclasses import dataclass

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import PHOTO_STORY_TIMEOUT_MINUTES
from core.models import Photo


class PhotoStory(StatesGroup):
    waiting = State()


@dataclass(frozen=True)
class PendingPhotoBatch:
    since: datetime.datetime
    diary_id: int


async def peek_pending_photo_batch(state: FSMContext) -> PendingPhotoBatch | None:
    """Check whether we're waiting for a photo story, without consuming the state.

    Callers that might fail to actually save the reply (e.g. quota exceeded)
    should not clear the pending batch themselves, so the user can retry
    within the same window.
    """
    if await state.get_state() != PhotoStory.waiting.state:
        return None

    data = await state.get_data()
    batch_start = datetime.datetime.fromisoformat(data["batch_start"])
    if datetime.datetime.now() - batch_start > datetime.timedelta(
        minutes=PHOTO_STORY_TIMEOUT_MINUTES
    ):
        await state.clear()
        return None

    return PendingPhotoBatch(since=batch_start, diary_id=data["diary_id"])


async def link_pending_photos(
    session: AsyncSession, user_id: int, diary_id: int, since: datetime.datetime, message_id: int
) -> int:
    result = await session.execute(
        update(Photo)
        .where(
            Photo.user_id == user_id,
            Photo.diary_id == diary_id,
            Photo.message_id.is_(None),
            Photo.created_at >= since,
        )
        .values(message_id=message_id)
    )
    return result.rowcount
