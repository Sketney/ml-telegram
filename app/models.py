from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TopicStatus(str, enum.Enum):
    NEW = "new"  # в бэклоге, ещё не брали
    USED = "used"  # по теме есть черновик или пост
    SKIPPED = "skipped"  # автор отклонил тему


class DraftStatus(str, enum.Enum):
    PENDING = "pending"  # ждёт решения автора
    APPROVED = "approved"  # одобрен, ждёт своего слота в расписании
    PUBLISHED = "published"
    POSTPONED = "postponed"  # отложен без даты
    REJECTED = "rejected"


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("rubric", "title", name="uq_topic_rubric_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rubric: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=TopicStatus.NEW.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rubric: Mapped[str] = mapped_column(String(32), index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=DraftStatus.PENDING.value, index=True)
    # post: {"kind": "post", "html": "..."}
    # quiz: {"kind": "quiz", "question": "...", "options": [...], "correct_index": 0, "explanation": "..."}
    payload: Mapped[dict] = mapped_column(JSON)
    # [{"level": "error"|"warn", "text": "..."}] — error блокирует публикацию
    issues: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    @property
    def has_errors(self) -> bool:
        return any(i.get("level") == "error" for i in (self.issues or []))
