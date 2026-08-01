from pathlib import Path

from core.config import settings


def voice_path(user_id: int, file_unique_id: str) -> Path:
    user_dir = Path(settings.media_root) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{file_unique_id}.ogg"
