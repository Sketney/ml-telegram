"""Дайджест кандидатов из источников: список по рубрикам и кнопки «Написать» / «Скрыть»."""

from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SourceItem
from app.rubrics import get_rubric
from app.sources import service as sources
from app.sources.collectors import SourcesConfig


class ItemAction(CallbackData, prefix="i"):
    action: str  # write | hide
    item_id: int


def _facts(item: SourceItem) -> str:
    meta = item.meta or {}
    parts = [f"{item.score}/10"]
    for key, label in (("upvotes", "▲"), ("stars", "★"), ("likes", "♥")):
        if meta.get(key):
            parts.append(f"{label}{meta[key]}")
    if meta.get("feed"):
        parts.append(str(meta["feed"]))
    return " · ".join(parts)


def digest_text(rubric_key: str, items: list[SourceItem]) -> str:
    lines = [f"<b>Кандидаты: {escape(get_rubric(rubric_key).title)}</b>"]
    for n, it in enumerate(items, 1):
        lines.append(
            f'\n{n}. <a href="{escape(it.url, quote=True)}">{escape(it.title[:160])}</a> — {escape(_facts(it))}'
        )
        if it.score_reason:
            lines.append(f"<i>{escape(it.score_reason)}</i>")
    return "\n".join(lines)


def digest_keyboard(items: list[SourceItem]) -> InlineKeyboardMarkup:
    def row(icon: str, action: str) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton(text=f"{icon} {n}", callback_data=ItemAction(action=action, item_id=it.id).pack())
            for n, it in enumerate(items, 1)
        ]

    return InlineKeyboardMarkup(inline_keyboard=[row("✍️", "write"), row("🚫", "hide")])


def without_item(markup: InlineKeyboardMarkup | None, item_id: int) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без кнопок обработанного кандидата."""
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        kept = [b for b in row if not (b.callback_data and ItemAction.unpack(b.callback_data).item_id == item_id)]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_digest(
    bot: Bot, chat_id: int, session: AsyncSession, cfg: SourcesConfig, *, include_offered: bool
) -> int:
    """Шлёт по сообщению на рубрику. Возвращает число показанных кандидатов."""
    by_rubric = await sources.digest_items(session, cfg, include_offered=include_offered)
    shown = 0
    for rubric_key, items in by_rubric.items():
        await bot.send_message(
            chat_id=chat_id,
            text=digest_text(rubric_key, items),
            parse_mode="HTML",
            reply_markup=digest_keyboard(items),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await sources.mark_offered(session, items)
        shown += len(items)
    return shown
