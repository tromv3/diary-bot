import datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import SOURCE_TEXT
from core.db import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text, server_default=SOURCE_TEXT)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    words_used: Mapped[int] = mapped_column(Integer, server_default="0")


class UserCredit(Base):
    __tablename__ = "user_credits"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bonus_words: Mapped[int] = mapped_column(Integer, server_default="0")
