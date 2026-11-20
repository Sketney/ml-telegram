"""Отправка черновика: превью автору и публикация в канал."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputPollOption, LinkPreviewOptions, Message

from app.models import Draft

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class NoImage(Exception):
    """У шпаргалки нет собранной картинки."""


async def send_draft(bot: Bot, chat_id: int | str, draft: Draft, image: bytes | None = None) -> Message:
    """Отправляет содержимое черновика в чат в том виде, в каком его увидят в канале."""
    p = draft.payload
    if p["kind"] == "sheet":
        if not image:
            raise NoImage("у шпаргалки нет картинки: перегенерируй черновик")
        return await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(image, filename=f"cheatsheet-{draft.id}.png"),
            caption=p["html"],
            parse_mode="HTML",
        )
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


async def send_preview(bot: Bot, chat_id: int | str, draft: Draft, image: bytes | None = None) -> Message:
    """Превью для автора. Если разметка сломана или нет картинки, показывает сырой текст, а не падает."""
    try:
        return await send_draft(bot, chat_id, draft, image)
    except NoImage as e:
        return await bot.send_message(
            chat_id=chat_id,
            text=f"{e}\n\nПодпись:\n{draft.payload.get('html')}"[:4096],
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
        )
    except TelegramBadRequest as e:
        p = draft.payload
        raw = p.get("html") or f"{p.get('question')}\n" + "\n".join(f"• {o}" for o in p.get("options") or [])
        return await bot.send_message(
            chat_id=chat_id,
            text=f"Telegram не принял черновик ({e.message}). Сырой текст:\n\n{raw}"[:4096],
            parse_mode=None,
            link_preview_options=NO_PREVIEW,
        )
