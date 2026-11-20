"""Отправка черновика: превью автору и публикация в канал."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputPollOption, LinkPreviewOptions, Message

from app.models import Draft

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_draft(bot: Bot, chat_id: int | str, draft: Draft) -> Message:
    """Отправляет содержимое черновика в чат в том виде, в каком его увидят в канале."""
    p = draft.payload
    if p["kind"] == "quiz":
        return await bot.send_poll(
            chat_id=chat_id,
            question=p["question"],
            options=[InputPollOption(text=o) for o in p["options"]],
            type="quiz",
            is_anonymous=True,
            correct_option_ids=[p["correct_index"]],
            explanation=p.get("explanation") or None,
        )
    return await bot.send_message(chat_id=chat_id, text=p["html"], parse_mode="HTML", link_preview_options=NO_PREVIEW)


async def send_preview(bot: Bot, chat_id: int | str, draft: Draft) -> Message:
    """Превью для автора. Если разметка сломана, показывает сырой текст, а не падает."""
    try:
        return await send_draft(bot, chat_id, draft)
    except TelegramBadRequest as e:
        p = draft.payload
        raw = p.get("html") or f"{p.get('question')}\n" + "\n".join(f"• {o}" for o in p.get("options") or [])
        return await bot.send_message(
            chat_id=chat_id,
            text=f"Telegram не принял черновик ({e.message}). Сырой текст:\n\n{raw}"[:4096],
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
        )
