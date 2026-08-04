import datetime
import re
from dataclasses import dataclass

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.constants import ANSWER_TYPE_NUMBER, ANSWER_TYPE_TEXT, QUESTION_TIMEOUT_MINUTES
from core.models import QuestionAskLog, UserSettings


@dataclass(frozen=True)
class QuestionDef:
    key: str
    text: str
    answer_type: str
    cadence_days: int


# Placeholder catalog, applies to every diary regardless of category. The real
# category -> question catalog (with per-category cadence) is a separate,
# later design round — this exists only to exercise the delivery mechanism
# (scheduling, sequential queueing, validation, voice confirm) end-to-end.
QUESTIONS: list[QuestionDef] = [
    QuestionDef("daily_mood", "Как прошёл день?", ANSWER_TYPE_TEXT, cadence_days=1),
    QuestionDef(
        "daily_water",
        "Сколько стаканов воды сегодня выпил? (просто число)",
        ANSWER_TYPE_NUMBER,
        cadence_days=1,
    ),
]


class QuestionFlow(StatesGroup):
    waiting = State()


@dataclass(frozen=True)
class PendingQuestion:
    key: str
    text: str
    answer_type: str
    diary_id: int
    pending_voice_text: str | None


async def peek_pending_question(state: FSMContext) -> PendingQuestion | None:
    """Check whether we're waiting for an answer to a scheduled question.

    Mirrors core.photos.peek_pending_photo_batch: does not consume the state,
    so a failed save (e.g. quota exceeded) doesn't burn the user's answer
    window. Silently drops the whole remaining queue on timeout — unsent
    questions never had their `last_asked_at` written, so they become due
    again on their own next cadence cycle.
    """
    if await state.get_state() != QuestionFlow.waiting.state:
        return None

    data = await state.get_data()
    asked_at = datetime.datetime.fromisoformat(data["asked_at"])
    if datetime.datetime.now() - asked_at > datetime.timedelta(minutes=QUESTION_TIMEOUT_MINUTES):
        await state.clear()
        return None

    current = data["queue"][0]
    return PendingQuestion(
        key=current["key"],
        text=current["text"],
        answer_type=current["answer_type"],
        diary_id=data["diary_id"],
        pending_voice_text=data.get("pending_voice_text"),
    )


_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?")


def parse_number(raw: str) -> float | None:
    match = _NUMBER_RE.match(raw.strip())
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


async def get_due_questions(
    session: AsyncSession, user_id: int, today: datetime.date
) -> list[QuestionDef]:
    rows = (
        await session.execute(
            select(QuestionAskLog.question_key, QuestionAskLog.last_asked_at).where(
                QuestionAskLog.user_id == user_id
            )
        )
    ).all()
    last_asked = {key: asked_at.date() for key, asked_at in rows}

    due = []
    for question in QUESTIONS:
        last = last_asked.get(question.key)
        if last is None or (today - last).days >= question.cadence_days:
            due.append(question)
    return due


async def mark_question_asked(session: AsyncSession, user_id: int, question_key: str) -> None:
    stmt = (
        pg_insert(QuestionAskLog)
        .values(user_id=user_id, question_key=question_key, last_asked_at=datetime.datetime.now())
        .on_conflict_do_update(
            index_elements=[QuestionAskLog.user_id, QuestionAskLog.question_key],
            set_={"last_asked_at": datetime.datetime.now()},
        )
    )
    await session.execute(stmt)


async def get_questions_enabled(session: AsyncSession, user_id: int) -> bool:
    settings_row = await session.get(UserSettings, user_id)
    return settings_row.questions_enabled if settings_row is not None else True


async def set_questions_enabled(session: AsyncSession, user_id: int, enabled: bool) -> None:
    await session.execute(
        update(UserSettings).where(UserSettings.user_id == user_id).values(questions_enabled=enabled)
    )
