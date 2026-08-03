from pathlib import Path

from core.config import settings


def _media_path(user_id: int, file_unique_id: str, extension: str) -> Path:
    user_dir = Path(settings.media_root) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{file_unique_id}.{extension}"


def voice_path(user_id: int, file_unique_id: str) -> Path:
    return _media_path(user_id, file_unique_id, "ogg")


def photo_path(user_id: int, file_unique_id: str) -> Path:
    return _media_path(user_id, file_unique_id, "jpg")
