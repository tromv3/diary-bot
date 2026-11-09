import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    openrouter_api_key: str
    openrouter_model: str
    redis_url: str
    media_root: str
    whisper_model_size: str
    whisper_device: str
    whisper_compute_type: str
    admin_ids: frozenset[int]
    admin_contact: str | None


def _load_settings() -> Settings:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill in your token."
        )

    pg_user = os.environ.get("POSTGRES_USER")
    pg_password = os.environ.get("POSTGRES_PASSWORD")
    pg_db = os.environ.get("POSTGRES_DB")
    pg_host = os.environ.get("POSTGRES_HOST", "localhost")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    if not all([pg_user, pg_password, pg_db]):
        raise RuntimeError(
            "POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB are not set. "
            "Copy .env.example to .env and fill them in."
        )
    database_url = (
        f"postgresql+asyncpg://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
    )

    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in "
            "(get a free key at https://openrouter.ai/keys)."
        )
    openrouter_model = os.environ.get(
        "OPENROUTER_MODEL", "openai/gpt-oss-20b:free"
    )

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = os.environ.get("REDIS_PORT", "6379")
    redis_url = f"redis://{redis_host}:{redis_port}/0"

    media_root = os.environ.get("MEDIA_ROOT", "media")
    whisper_model_size = os.environ.get("WHISPER_MODEL_SIZE", "large-v3")
    whisper_device = os.environ.get("WHISPER_DEVICE", "cuda")
    whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")

    admin_ids_raw = os.environ.get("ADMIN_IDS", "")
    admin_ids = frozenset(
        int(part) for part in admin_ids_raw.split(",") if part.strip()
    )
    admin_contact = os.environ.get("ADMIN_CONTACT") or None

    return Settings(
        bot_token=token,
        database_url=database_url,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        redis_url=redis_url,
        media_root=media_root,
        whisper_model_size=whisper_model_size,
        whisper_device=whisper_device,
        whisper_compute_type=whisper_compute_type,
        admin_ids=admin_ids,
        admin_contact=admin_contact,
    )


settings = _load_settings()
