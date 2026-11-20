"""Просмотры постов с публичной страницы канала t.me/s/<канал>: без входа в аккаунт и без API-ключей."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Draft, DraftStatus, PostStats, Topic, utcnow
from app.rubrics import load_rubrics
from app.sources.http import FetchError, fetch_text

log = logging.getLogger(__name__)

MAX_PAGES = 6  # страница — около 20 последних постов
_POST_RE = re.compile(r'data-post="[^"/]+/(\d+)"(.*?)(?=data-post="|\Z)', re.DOTALL)
_VIEWS_RE = re.compile(r'tgme_widget_message_views">([^<]+)<')
# Браузерный User-Agent: без него страница отдаёт заглушку вместо ленты.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ml-telegram-bot/0.3; +https://github.com/Sketney/ml-telegram)"}


class StatsUnavailable(Exception):
    pass


def parse_count(text: str) -> int | None:
    """«2.32K» → 2320, «1.2M» → 1200000, «845» → 845."""
    m = re.fullmatch(r"\s*([\d.,]+)\s*([KkMm]?)\s*", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return round(value * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()])


def parse_channel_page(html: str) -> dict[int, int]:
    """message_id → просмотры для постов на странице."""
    views: dict[int, int] = {}
    for message_id, block in _POST_RE.findall(html):
        found = _VIEWS_RE.search(block)
        count = parse_count(found.group(1)) if found else None
        if count is not None:
            views[int(message_id)] = count
    return views


def channel_username(settings: Settings) -> str:
    cid = settings.channel_id.strip()
    if not cid.startswith("@"):
        raise StatsUnavailable("статистика читается с публичной страницы канала: в CHANNEL_ID нужен @username")
    return cid[1:]


async def refresh(session: AsyncSession, settings: Settings) -> int:
    """Обновляет просмотры опубликованных постов. Возвращает, для скольких постов нашлись данные."""
    username = channel_username(settings)
    wanted = dict(
        (
            await session.execute(
                select(Draft.channel_message_id, Draft.id).where(
                    Draft.status == DraftStatus.PUBLISHED.value, Draft.channel_message_id.is_not(None)
                )
            )
        ).all()
    )
    if not wanted:
        return 0
    seen: dict[int, int] = {}
    before: int | None = None
    for _ in range(MAX_PAGES):
        url = f"https://t.me/s/{username}" + (f"?before={before}" if before else "")
        try:
            page = parse_channel_page(await fetch_text(url, headers=_HEADERS))
        except FetchError as e:
            if not seen:
                raise StatsUnavailable(str(e)) from e
            break
        new = {k: v for k, v in page.items() if k not in seen}
        if not new:
            break
        seen.update(new)
        before = min(new)
        if before <= min(wanted):
            break
    updated = 0
    for message_id, draft_id in wanted.items():
        if message_id not in seen:
            continue
        stats = await session.get(PostStats, draft_id)
        if stats is None:
            session.add(PostStats(draft_id=draft_id, views=seen[message_id]))
        else:
            stats.views, stats.updated_at = seen[message_id], utcnow()
        updated += 1
    await session.commit()
    return updated


@dataclass(frozen=True)
class RubricStats:
    title: str
    posts: int
    avg_views: float


async def summary(session: AsyncSession, top: int = 5) -> tuple[list[RubricStats], list[tuple[str, str, int]], int]:
    """Средние просмотры по рубрикам, лучшие посты (рубрика, тема, просмотры) и число постов без данных."""
    rubrics = load_rubrics()
    rows = (
        await session.execute(
            select(Draft.rubric, func.count(), func.avg(PostStats.views))
            .join(PostStats, PostStats.draft_id == Draft.id)
            .group_by(Draft.rubric)
        )
    ).all()
    by_rubric = sorted(
        (RubricStats(rubrics[r].title if r in rubrics else r, n, float(avg)) for r, n, avg in rows),
        key=lambda x: -x.avg_views,
    )
    best = (
        await session.execute(
            select(Draft.rubric, Topic.title, PostStats.views)
            .join(PostStats, PostStats.draft_id == Draft.id)
            .join(Topic, Topic.id == Draft.topic_id, isouter=True)
            .order_by(PostStats.views.desc(), Draft.id.desc())
            .limit(top)
        )
    ).all()
    top_posts = [(rubrics[r].title if r in rubrics else r, title or "без темы", views) for r, title, views in best]
    published = await session.scalar(
        select(func.count()).select_from(Draft).where(Draft.status == DraftStatus.PUBLISHED.value)
    )
    with_stats = await session.scalar(select(func.count()).select_from(PostStats))
    return by_rubric, top_posts, (published or 0) - (with_stats or 0)
