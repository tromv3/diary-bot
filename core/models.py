import datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.constants import CATEGORY_GENERAL, SOURCE_TEXT
from core.db import Base


class Diary(Base):
    __tablename__ = "diaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, server_default=CATEGORY_GENERAL)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    active_diary_id: Mapped[int] = mapped_column(ForeignKey("diaries.id"))
    pinned_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    questions_enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")


class QuestionAskLog(Base):
    __tablename__ = "question_ask_log"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_asked_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    diary_id: Mapped[int] = mapped_column(ForeignKey("diaries.id"), index=True)
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
    diary_id: Mapped[int] = mapped_column(ForeignKey("diaries.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    words_used: Mapped[int] = mapped_column(Integer, server_default="0")


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    diary_id: Mapped[int] = mapped_column(ForeignKey("diaries.id"), index=True)
    media_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserCredit(Base):
    __tablename__ = "user_credits"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bonus_words: Mapped[int] = mapped_column(Integer, server_default="0")
