"""Кандидаты из источников: сохранение, оценка дешёвой моделью, выборка для дайджеста, текст-опора."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROMPTS_DIR
from app.jsonutil import extract_json_object
from app.llm import LLM
from app.models import ItemStatus, SourceItem, Topic, TopicStatus
from app.rubrics import load_rubrics
from app.sources.collectors import Found, SourcesConfig, collect
from app.sources.http import FetchError, clean_markdown, fetch_text, html_to_text

log = logging.getLogger(__name__)

RANK_BATCH = 40  # материалов в одном запросе к модели
RANK_MAX_BATCHES = 4  # на рубрику за один запуск: потолок расходов при первом сборе
GROUNDING_LIMIT = 15000  # символов текста источника в промпте
PAPER_LIMIT = 60000  # статье нужен весь основной текст: метод и ограничения стоят не в начале
PAPER_MIN_BODY = 3000  # короче — это не текст статьи, а заглушка или страница ошибки
_REFERENCES_RE = re.compile(r"^\s*(References|Bibliography)\s*$", re.MULTILINE)
_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass
class CollectReport:
    new: int = 0
    ranked: int = 0
    errors: list[str] = field(default_factory=list)


# --- сохранение и оценка --------------------------------------------------------


async def store_found(session: AsyncSession, found: list[Found]) -> int:
    """Сохраняет новые материалы; уже известные (по url) пропускает. Возвращает число новых."""
    if not found:
        return 0
    urls = [f.url for f in found]
    known = set((await session.scalars(select(SourceItem.url).where(SourceItem.url.in_(urls)))).all())
    rubrics = load_rubrics()
    added = 0
    for f in found:
        if f.url in known or f.rubric not in rubrics:
            continue
        session.add(
            SourceItem(
                source=f.source,
                rubric=f.rubric,
                url=f.url,
                title=f.title,
                summary=f.summary,
                meta=f.meta,
                published_at=f.published_at,
            )
        )
        known.add(f.url)
        added += 1
    await session.commit()
    return added


def _rank_system_prompt() -> str:
    return (PROMPTS_DIR / "ranking.md").read_text(encoding="utf-8").strip()


def _rank_payload(items: list[SourceItem]) -> str:
    rows = []
    for it in items:
        meta = {k: v for k, v in (it.meta or {}).items() if k in ("upvotes", "likes", "downloads", "stars", "feed")}
        rows.append({"id": it.id, "title": it.title, "summary": it.summary[:600], **meta})
    return json.dumps(rows, ensure_ascii=False)


KNOWN_TOPICS_LIMIT = 80


async def known_topics(session: AsyncSession) -> list[str]:
    """Темы, по которым уже есть черновик или пост: чтобы не предлагать то же самое второй раз."""
    rows = await session.scalars(
        select(Topic.title)
        .where(Topic.status == TopicStatus.USED.value)
        .order_by(Topic.id.desc())
        .limit(KNOWN_TOPICS_LIMIT)
    )
    return list(rows.all())


async def rank_new(session: AsyncSession, llm: LLM) -> int:
    """Оценивает ещё не оценённые материалы. Возвращает, сколько оценено."""
    ranked = 0
    system = _rank_system_prompt()
    known = await known_topics(session)
    known_block = (
        "Темы, о которых канал уже писал или пишет:\n" + "\n".join(f"- {t}" for t in known) + "\n\n" if known else ""
    )
    for rubric in load_rubrics().values():
        if rubric.source != "feed":
            continue
        for _ in range(RANK_MAX_BATCHES):
            items = list(
                (
                    await session.scalars(
                        select(SourceItem)
                        .where(SourceItem.rubric == rubric.key, SourceItem.status == ItemStatus.NEW.value)
                        .order_by(SourceItem.id)
                        .limit(RANK_BATCH)
                    )
                ).all()
            )
            if not items:
                break
            user = f"Рубрика: {rubric.key} ({rubric.title})\n\n{known_block}Материалы:\n{_rank_payload(items)}"
            try:
                data = extract_json_object(await llm.complete(system, user, cheap=True))
            except ValueError as e:
                log.warning("оценка рубрики %s: %s", rubric.key, e)
                break  # материалы остаются NEW и будут оценены в следующий запуск
            scores = {}
            for row in data.get("items") or []:
                try:
                    scores[int(row["id"])] = (max(0, min(10, int(row["score"]))), str(row.get("why") or "")[:380])
                except (KeyError, TypeError, ValueError):
                    continue
            for it in items:
                it.score, it.score_reason = scores.get(it.id, (0, "модель не вернула оценку"))
                it.status = ItemStatus.RANKED.value
            await session.commit()
            ranked += len(items)
    return ranked


async def collect_and_rank(session: AsyncSession, llm: LLM, cfg: SourcesConfig) -> CollectReport:
    found, errors = await collect(cfg)
    report = CollectReport(errors=errors)
    report.new = await store_found(session, found)
    report.ranked = await rank_new(session, llm)
    return report


# --- дайджест -------------------------------------------------------------------


async def digest_items(
    session: AsyncSession, cfg: SourcesConfig, *, include_offered: bool, now: datetime | None = None
) -> dict[str, list[SourceItem]]:
    """Лучшие кандидаты по рубрикам. include_offered=False — только те, что автор ещё не видел."""
    now = now or datetime.now(timezone.utc)
    statuses = [ItemStatus.RANKED.value] + ([ItemStatus.OFFERED.value] if include_offered else [])
    out: dict[str, list[SourceItem]] = {}
    for rubric in load_rubrics().values():
        if rubric.source != "feed":
            continue
        items = list(
            (
                await session.scalars(
                    select(SourceItem)
                    .where(
                        SourceItem.rubric == rubric.key,
                        SourceItem.status.in_(statuses),
                        SourceItem.score >= cfg.min_score,
                        SourceItem.fetched_at >= now - timedelta(days=cfg.max_age_days),
                    )
                    .order_by(
                        SourceItem.score.desc(), SourceItem.published_at.desc().nulls_last(), SourceItem.id.desc()
                    )
                    .limit(cfg.digest_top)
                )
            ).all()
        )
        if items:
            out[rubric.key] = items
    return out


async def mark_offered(session: AsyncSession, items: list[SourceItem]) -> None:
    for it in items:
        if it.status == ItemStatus.RANKED.value:
            it.status = ItemStatus.OFFERED.value
    await session.commit()


async def item_for_topic(session: AsyncSession, topic_id: int | None) -> SourceItem | None:
    if topic_id is None:
        return None
    return await session.scalar(select(SourceItem).where(SourceItem.topic_id == topic_id).limit(1))


# --- текст-опора для генерации ---------------------------------------------------


def _clip(text: str, limit: int = GROUNDING_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[текст обрезан]"


def _paper_body(body: str) -> str:
    """Текст статьи без списка литературы и приложений: они съедают лимит и для разбора не нужны."""
    cut = None
    for m in _REFERENCES_RE.finditer(body):
        cut = m.start()
    return body[:cut].rstrip() if cut and cut > PAPER_MIN_BODY else body


async def grounding_for(item: SourceItem) -> tuple[str, str]:
    """Текст источника для промпта и пометка, насколько он полон.

    Сеть может подвести: тогда остаёмся с тем, что собрали раньше (аннотация, описание).
    """
    meta = item.meta or {}
    try:
        if item.source == "hf_papers" and meta.get("arxiv_id"):
            body = html_to_text(await fetch_text(f"https://arxiv.org/html/{meta['arxiv_id']}"))
            if len(body) > PAPER_MIN_BODY:
                text = _clip(f"Аннотация:\n{item.summary}\n\nТекст статьи:\n{_paper_body(body)}", PAPER_LIMIT)
                clipped = text.endswith("[текст обрезан]")
                return text, "аннотация и текст статьи без концовки" if clipped else "аннотация и текст статьи"
        elif item.source == "hf_models" and meta.get("model_id"):
            readme = await fetch_text(f"https://huggingface.co/{meta['model_id']}/raw/main/README.md")
            readme = clean_markdown(_FRONT_MATTER_RE.sub("", readme))
            if readme:
                return _clip(readme), "карточка модели (README)"
        elif item.source == "github" and meta.get("repo"):
            readme = clean_markdown(
                await fetch_text(f"https://raw.githubusercontent.com/{meta['repo']}/HEAD/README.md")
            )
            if readme:
                return _clip(f"Описание: {item.summary}\n\nREADME:\n{readme}"), "README репозитория"
        elif item.source.startswith("rss:"):
            body = html_to_text(await fetch_text(item.url))
            if len(body) > 1500:
                return _clip(body), "текст страницы"
    except FetchError as e:
        log.warning("не удалось получить полный текст %s: %s", item.url, e)
    kind = "только аннотация" if item.source == "hf_papers" else "только краткое описание"
    return _clip(item.summary or item.title), kind
