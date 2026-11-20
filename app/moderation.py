"""Действия автора над черновиком. Хендлеры бота и планировщик зовут эти функции."""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import course, covers
from app.config import Settings
from app.models import Draft, DraftImage, DraftStatus, Topic, TopicStatus, utcnow
from app.publisher import NoImage, send_draft
from app.rubrics import get_rubric
from app.validator import find_placeholders, validate_payload

log = logging.getLogger(__name__)


class NotAllowed(Exception):
    """Действие невозможно в текущем состоянии черновика; текст — причина для автора."""


OPEN_STATUSES = (DraftStatus.PENDING.value, DraftStatus.APPROVED.value, DraftStatus.POSTPONED.value)

# Рубрики, у которых есть слот в расписании. None — расписание не загружено (тесты, скрипты): не ограничиваем.
_slotted: set[str] | None = None


def set_slotted_rubrics(rubrics: set[str] | None) -> None:
    global _slotted
    _slotted = rubrics


def has_slot(rubric: str) -> bool:
    return _slotted is None or rubric in _slotted


def channel_chat_id(settings: Settings) -> int | str:
    cid = settings.channel_id.strip()
    return int(cid) if cid.lstrip("-").isdigit() else cid


def ensure_open(draft: Draft) -> None:
    if draft.status not in OPEN_STATUSES:
        raise NotAllowed(f"черновик уже в статусе «{draft.status}»")


def _ensure_publishable(draft: Draft) -> None:
    ensure_open(draft)
    if draft.has_errors:
        errors = "; ".join(i["text"] for i in draft.issues if i["level"] == "error")
        raise NotAllowed(f"сначала нужно исправить: {errors}")


async def approve(session: AsyncSession, draft: Draft) -> Draft:
    """В очередь: выйдет в ближайший слот своей рубрики."""
    _ensure_publishable(draft)
    if not has_slot(draft.rubric):
        raise NotAllowed(
            f"у рубрики «{get_rubric(draft.rubric).title}» нет слота в расписании: "
            "публикуй кнопкой «Сейчас» или добавь слот в data/schedule.yaml"
        )
    draft.status = DraftStatus.APPROVED.value
    draft.approved_at = utcnow()
    await session.commit()
    return draft


async def draft_image(session: AsyncSession, draft: Draft) -> bytes | None:
    if draft.payload.get("kind") != "sheet":
        return None
    image = await session.get(DraftImage, draft.id)
    return image.png if image else None


async def publish(session: AsyncSession, bot: Bot, settings: Settings, draft: Draft) -> Draft:
    _ensure_publishable(draft)
    try:
        msg = await send_draft(
            bot, channel_chat_id(settings), draft, await draft_image(session, draft), await covers.for_draft(draft)
        )
    except NoImage as e:
        raise NotAllowed(str(e)) from e
    draft.status = DraftStatus.PUBLISHED.value
    draft.published_at = utcnow()
    draft.channel_message_id = msg.message_id
    await session.commit()
    if get_rubric(draft.rubric).series:
        await _update_course_toc(session, bot, settings, draft)
    return draft


async def _update_course_toc(session: AsyncSession, bot: Bot, settings: Settings, draft: Draft) -> None:
    """Оглавление курса обновляется после выхода урока. Сбой здесь публикацию урока не отменяет."""
    try:
        note = await course.update_toc(session, bot, settings, draft.rubric, channel_chat_id(settings))
    except Exception as e:
        log.exception("оглавление курса не обновилось")
        note = f"Урок вышел, но оглавление курса не обновилось: {e}"
    if note:
        try:
            await bot.send_message(settings.admin_id, note)
        except Exception:
            log.exception("не удалось написать автору: %s", note)


async def postpone(session: AsyncSession, draft: Draft) -> Draft:
    ensure_open(draft)
    draft.status = DraftStatus.POSTPONED.value
    await session.commit()
    return draft


async def reject(session: AsyncSession, draft: Draft) -> Draft:
    """В архив; тема помечается, чтобы бот не предложил её снова.

    Урок курса пропустить нельзя: отклонён черновик, а не урок, поэтому он возвращается в очередь.
    """
    ensure_open(draft)
    draft.status = DraftStatus.REJECTED.value
    if draft.topic_id:
        topic = await session.get(Topic, draft.topic_id)
        if topic:
            series = get_rubric(draft.rubric).series
            topic.status = TopicStatus.NEW.value if series else TopicStatus.SKIPPED.value
    await session.commit()
    return draft


async def set_text(session: AsyncSession, draft: Draft, html: str) -> Draft:
    """Автор прислал свою версию текста поста."""
    ensure_open(draft)
    if draft.payload.get("kind") not in ("post", "sheet"):
        raise NotAllowed("правка текстом работает только для постов; квиз можно перегенерировать с комментарием")
    rubric = get_rubric(draft.rubric)
    payload = {**draft.payload, "html": html.strip()}  # у шпаргалки меняется только подпись, картинка остаётся
    draft.payload = payload
    draft.issues = validate_payload(payload, min_len=rubric.min_len, max_len=rubric.max_len)
    draft.version += 1
    draft.status = DraftStatus.PENDING.value
    await session.commit()
    return draft


async def fill_placeholder(session: AsyncSession, draft: Draft, placeholder: str, html_fragment: str) -> Draft:
    """Подставляет текст автора вместо заглушки вида {Мой опыт}."""
    ensure_open(draft)
    html = draft.payload.get("html") or ""
    if draft.payload.get("kind") != "post" or placeholder not in find_placeholders(html):
        raise NotAllowed(f"в черновике уже нет заглушки {placeholder}")
    return await set_text(session, draft, html.replace(placeholder, html_fragment.strip(), 1))


async def next_approved(session: AsyncSession, rubrics: str | tuple[str, ...]) -> Draft | None:
    """Самый давно одобренный черновик из перечисленных рубрик."""
    keys = (rubrics,) if isinstance(rubrics, str) else tuple(rubrics)
    return await session.scalar(
        select(Draft)
        .where(Draft.rubric.in_(keys), Draft.status == DraftStatus.APPROVED.value)
        .order_by(Draft.approved_at, Draft.id)
        .limit(1)
    )


async def count_in_work(session: AsyncSession, rubric: str) -> int:
    """Сколько черновиков рубрики ждут решения или своего слота."""
    return await session.scalar(
        select(func.count())
        .select_from(Draft)
        .where(Draft.rubric == rubric, Draft.status.in_((DraftStatus.PENDING.value, DraftStatus.APPROVED.value)))
    )


async def list_queue(session: AsyncSession) -> list[Draft]:
    rows = await session.scalars(select(Draft).where(Draft.status.in_(OPEN_STATUSES)).order_by(Draft.status, Draft.id))
    return list(rows.all())
