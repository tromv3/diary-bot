import datetime
from dataclasses import dataclass

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from core.constants import VOICE_CONFIRM_TIMEOUT_MINUTES


class VoiceConfirm(StatesGroup):
    waiting = State()


@dataclass(frozen=True)
class PendingVoiceConfirm:
    diary_id: int
    linked_since: datetime.datetime | None
    transcript: str | None


async def peek_pending_voice_confirm(state: FSMContext) -> PendingVoiceConfirm | None:
    """Check whether a voice message is being transcribed/confirmed.

    `transcript` is None while the arq job is still running — the state is set
    up front (before transcription starts) so a stray message in that window
    doesn't race a fresh flow into the same FSM slot. Mirrors core.photos and
    core.questions: peek-only, timeout drops the whole thing silently.
    """
    if await state.get_state() != VoiceConfirm.waiting.state:
        return None

    data = await state.get_data()
    started_at = datetime.datetime.fromisoformat(data["started_at"])
    if datetime.datetime.now() - started_at > datetime.timedelta(
        minutes=VOICE_CONFIRM_TIMEOUT_MINUTES
    ):
        await state.clear()
        return None

    linked_since = data.get("linked_since")
    return PendingVoiceConfirm(
        diary_id=data["diary_id"],
        linked_since=datetime.datetime.fromisoformat(linked_since) if linked_since else None,
        transcript=data.get("transcript"),
    )
