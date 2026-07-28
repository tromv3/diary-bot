import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str


def _load_settings() -> Settings:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill in your token."
        )

    pg_user = os.environ.get("POSTGRES_USER")
    pg_password = os.environ.get("POSTGRES_PASSWORD")
    pg_db = os.environ.get("POSTGRES_DB")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    if not all([pg_user, pg_password, pg_db]):
        raise RuntimeError(
            "POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB are not set. "
            "Copy .env.example to .env and fill them in."
        )
    database_url = (
        f"postgresql+asyncpg://{pg_user}:{pg_password}@localhost:{pg_port}/{pg_db}"
    )

    return Settings(bot_token=token, database_url=database_url)


settings = _load_settings()
