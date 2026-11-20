"""Карточка черновика: превью + сообщение с кнопками."""

from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Draft, DraftStatus, Topic
from app.publisher import send_preview
from app.rubrics import get_rubric


class DraftAction(CallbackData, prefix="d"):
    action: str  # pub | ok | edit | regen | regenc | later | no
    draft_id: int


STATUS_LABELS = {
    DraftStatus.PENDING.value: "ждёт решения",
    DraftStatus.APPROVED.value: "в очереди на публикацию",
    DraftStatus.PUBLISHED.value: "опубликован",
    DraftStatus.POSTPONED.value: "отложен",
    DraftStatus.REJECTED.value: "отклонён",
}


def draft_keyboard(draft: Draft) -> InlineKeyboardMarkup | None:
    if draft.status in (DraftStatus.PUBLISHED.value, DraftStatus.REJECTED.value):
        return None

    def btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=DraftAction(action=action, draft_id=draft.id).pack())

    rows: list[list[InlineKeyboardButton]] = []
    if not draft.has_errors:
        first = [btn("✅ Сейчас", "pub")]
        if draft.status != DraftStatus.APPROVED.value:
            first.append(btn("🕒 В расписание", "ok"))
        rows.append(first)
    second = [btn("🔄 Заново", "regen"), btn("💬 С комментарием", "regenc")]
    if draft.payload.get("kind") == "post":
        second.insert(0, btn("✏️ Править", "edit"))
    rows.append(second)
    last = [btn("❌ Отклонить", "no")]
    if draft.status != DraftStatus.POSTPONED.value:
        last.insert(0, btn("⏸ Отложить", "later"))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def card_text(session: AsyncSession, draft: Draft, note: str = "") -> str:
    rubric = get_rubric(draft.rubric)
    topic = await session.get(Topic, draft.topic_id) if draft.topic_id else None
    lines = [
        f"<b>Черновик #{draft.id}</b> · {escape(rubric.title)} · v{draft.version}",
        f"Статус: {STATUS_LABELS.get(draft.status, draft.status)}",
    ]
    if topic:
        lines.insert(1, f"Тема: {escape(topic.title)}")
    for i in draft.issues or []:
        mark = "⛔️" if i["level"] == "error" else "⚠️"
        lines.append(f"{mark} {escape(i['text'])}")
    if draft.has_errors:
        lines.append("Публикация закрыта, пока есть ⛔️: поправь текст или перегенерируй.")
    if note:
        lines.append(f"\n{escape(note)}")
    return "\n".join(lines)


async def send_card(bot: Bot, chat_id: int, session: AsyncSession, draft: Draft, note: str = "") -> Message:
    """Превью (как в канале) и следом сообщение с кнопками."""
    await send_preview(bot, chat_id, draft)
    return await bot.send_message(
        chat_id=chat_id,
        text=await card_text(session, draft, note),
        parse_mode="HTML",
        reply_markup=draft_keyboard(draft),
    )
