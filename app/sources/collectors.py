"""Сбор кандидатов из внешних источников.

Разбор ответов (parse_*) отделён от сети, чтобы его можно было проверять на сохранённых примерах.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import yaml

from app.sources.http import FetchError, fetch_json, fetch_text, html_to_text

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 2000
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Found:
    source: str
    rubric: str
    url: str
    title: str
    summary: str = ""
    meta: dict = field(default_factory=dict)
    published_at: datetime | None = None


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    rubric: str


@dataclass(frozen=True)
class SourcesConfig:
    collect_hour: int = 16
    collect_minute: int = 0
    digest_top: int = 3
    min_score: int = 6
    max_age_days: int = 14

    hf_papers: bool = True
    hf_papers_limit: int = 50

    hf_models: bool = True
    hf_models_limit: int = 30
    hf_models_pipeline_tags: tuple[str, ...] = ("text-generation", "image-text-to-text")
    hf_models_min_likes: int = 50
    hf_models_max_age_days: int = 45

    github: bool = True
    github_queries: tuple[str, ...] = ("topic:llm", "topic:rag", "topic:ai-agents", "topic:mlops")
    github_created_within_days: int = 60
    github_min_stars: int = 500
    github_per_query: int = 10

    feeds: tuple[Feed, ...] = ()
    feed_limit: int = 20

    @property
    def enabled(self) -> bool:
        return self.hf_papers or self.hf_models or self.github or bool(self.feeds)


def load_sources_config(path: str | Path) -> SourcesConfig:
    path = Path(path)
    if not path.exists():
        return SourcesConfig(hf_papers=False, hf_models=False, github=False)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    hour, minute = (int(x) for x in str(raw.get("collect_at", "16:00")).split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"sources.yaml: collect_at — время «{raw.get('collect_at')}», ожидается ЧЧ:ММ")
    papers, models, github = (raw.get(k) or {} for k in ("hf_papers", "hf_models", "github"))
    default = SourcesConfig()
    return SourcesConfig(
        collect_hour=hour,
        collect_minute=minute,
        digest_top=int(raw.get("digest_top", default.digest_top)),
        min_score=int(raw.get("min_score", default.min_score)),
        max_age_days=int(raw.get("max_age_days", default.max_age_days)),
        hf_papers=bool(papers.get("enabled", False)),
        hf_papers_limit=int(papers.get("limit", default.hf_papers_limit)),
        hf_models=bool(models.get("enabled", False)),
        hf_models_limit=int(models.get("limit", default.hf_models_limit)),
        hf_models_pipeline_tags=tuple(models.get("pipeline_tags", default.hf_models_pipeline_tags)),
        hf_models_min_likes=int(models.get("min_likes", default.hf_models_min_likes)),
        hf_models_max_age_days=int(models.get("max_age_days", default.hf_models_max_age_days)),
        github=bool(github.get("enabled", False)),
        github_queries=tuple(github.get("queries", default.github_queries)),
        github_created_within_days=int(github.get("created_within_days", default.github_created_within_days)),
        github_min_stars=int(github.get("min_stars", default.github_min_stars)),
        github_per_query=int(github.get("per_query", default.github_per_query)),
        feeds=tuple(Feed(name=f["name"], url=f["url"], rubric=f["rubric"]) for f in raw.get("rss") or []),
        feed_limit=int(raw.get("rss_limit", default.feed_limit)),
    )


# --- разбор дат и текста ------------------------------------------------------


def parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 (API, Atom) или RFC 822 (RSS). Не разобралось — None."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clip(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fresh(dt: datetime | None, max_age_days: int, now: datetime) -> bool:
    return dt is None or dt >= now - timedelta(days=max_age_days)


# --- Hugging Face: статьи дня ---------------------------------------------------


def parse_hf_papers(data: list) -> list[Found]:
    found = []
    for entry in data or []:
        paper = entry.get("paper") or {}
        arxiv_id, title = paper.get("id"), paper.get("title") or entry.get("title")
        if not arxiv_id or not title:
            continue
        found.append(
            Found(
                source="hf_papers",
                rubric="paper",
                url=f"https://huggingface.co/papers/{arxiv_id}",
                title=clip(title, 480),
                summary=clip(paper.get("summary") or entry.get("summary") or ""),
                meta={
                    "arxiv_id": arxiv_id,
                    "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}",
                    "upvotes": paper.get("upvotes"),
                    "authors": [a.get("name") for a in (paper.get("authors") or [])[:6] if a.get("name")],
                    "project_page": paper.get("projectPage") or None,
                },
                published_at=parse_dt(paper.get("publishedAt") or entry.get("publishedAt")),
            )
        )
    return found


# --- Hugging Face: модели в тренде ----------------------------------------------


def parse_hf_models(data: list, *, min_likes: int, max_age_days: int, now: datetime) -> list[Found]:
    found = []
    for model in data or []:
        model_id = model.get("id") or model.get("modelId")
        created = parse_dt(model.get("createdAt"))
        if not model_id or model.get("private"):
            continue
        if (model.get("likes") or 0) < min_likes or not _fresh(created, max_age_days, now):
            continue
        tags = [t for t in (model.get("tags") or []) if not t.startswith(("region:", "arxiv:"))][:20]
        found.append(
            Found(
                source="hf_models",
                rubric="release",
                url=f"https://huggingface.co/{model_id}",
                title=model_id,
                summary=clip("Модель на Hugging Face. Теги: " + ", ".join(tags)) if tags else "",
                meta={
                    "model_id": model_id,
                    "likes": model.get("likes"),
                    "downloads": model.get("downloads"),
                    "pipeline_tag": model.get("pipeline_tag"),
                    "library": model.get("library_name"),
                },
                published_at=created,
            )
        )
    return found


# --- GitHub: новые репозитории ---------------------------------------------------


def parse_github(data: dict) -> list[Found]:
    found = []
    for repo in (data or {}).get("items") or []:
        if repo.get("fork") or repo.get("archived") or not repo.get("html_url"):
            continue
        found.append(
            Found(
                source="github",
                rubric="tool",
                url=repo["html_url"],
                title=repo.get("full_name") or repo["html_url"],
                summary=clip(repo.get("description") or ""),
                meta={
                    "repo": repo.get("full_name"),
                    "stars": repo.get("stargazers_count"),
                    "language": repo.get("language"),
                    "topics": (repo.get("topics") or [])[:12],
                    "license": (repo.get("license") or {}).get("spdx_id"),
                },
                published_at=parse_dt(repo.get("created_at")),
            )
        )
    return found


# --- RSS и Atom ------------------------------------------------------------------


def _text(node: ET.Element | None) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def parse_feed(xml_text: str, feed: Feed, *, limit: int, max_age_days: int, now: datetime) -> list[Found]:
    root = ET.fromstring(xml_text.lstrip("﻿").strip())
    entries: list[tuple[str, str, str, str | None]] = []  # title, link, summary, date
    if root.tag == f"{_ATOM}feed":
        for e in root.findall(f"{_ATOM}entry"):
            links = e.findall(f"{_ATOM}link")
            link = next((x.get("href") for x in links if x.get("rel", "alternate") == "alternate"), None)
            link = link or (links[0].get("href") if links else None)
            summary = _text(e.find(f"{_ATOM}summary")) or _text(e.find(f"{_ATOM}content"))
            date = _text(e.find(f"{_ATOM}published")) or _text(e.find(f"{_ATOM}updated"))
            entries.append((_text(e.find(f"{_ATOM}title")), link or "", summary, date))
    else:
        for item in root.iter("item"):
            date = _text(item.find("pubDate")) or _text(item.find("{http://purl.org/dc/elements/1.1/}date"))
            entries.append((_text(item.find("title")), _text(item.find("link")), _text(item.find("description")), date))

    found = []
    for title, link, summary, date in entries[:limit]:
        published = parse_dt(date)
        if not title or not link.startswith("http") or not _fresh(published, max_age_days, now):
            continue
        found.append(
            Found(
                source=f"rss:{feed.name}"[:64],
                rubric=feed.rubric,
                url=link.strip()[:600],
                title=clip(html_to_text(title), 480),
                summary=clip(html_to_text(summary)),
                meta={"feed": feed.name},
                published_at=published,
            )
        )
    return found


# --- сбор --------------------------------------------------------------------------


async def collect(cfg: SourcesConfig, now: datetime | None = None) -> tuple[list[Found], list[str]]:
    """Обходит включённые источники. Сбой одного источника не мешает остальным."""
    now = now or datetime.now(timezone.utc)
    found: list[Found] = []
    errors: list[str] = []

    async def attempt(label: str, coro) -> None:
        try:
            found.extend(await coro)
        except (FetchError, ET.ParseError, ValueError, KeyError, TypeError) as e:
            log.warning("источник %s: %s", label, e)
            errors.append(f"{label}: {e}"[:300])

    async def hf_papers() -> list[Found]:
        return parse_hf_papers(await fetch_json(f"https://huggingface.co/api/daily_papers?limit={cfg.hf_papers_limit}"))

    async def hf_models(tag: str) -> list[Found]:
        url = (
            "https://huggingface.co/api/models?sort=trendingScore&direction=-1"
            f"&limit={cfg.hf_models_limit}&pipeline_tag={quote(tag)}"
        )
        return parse_hf_models(
            await fetch_json(url),
            min_likes=cfg.hf_models_min_likes,
            max_age_days=cfg.hf_models_max_age_days,
            now=now,
        )

    async def github(query: str) -> list[Found]:
        since = (now - timedelta(days=cfg.github_created_within_days)).date().isoformat()
        q = quote(f"{query} created:>{since} stars:>{cfg.github_min_stars}")
        url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={cfg.github_per_query}"
        return parse_github(await fetch_json(url, headers={"Accept": "application/vnd.github+json"}))

    async def feed(f: Feed) -> list[Found]:
        return parse_feed(await fetch_text(f.url), f, limit=cfg.feed_limit, max_age_days=cfg.max_age_days, now=now)

    if cfg.hf_papers:
        await attempt("HF Papers", hf_papers())
    if cfg.hf_models:
        for tag in cfg.hf_models_pipeline_tags:
            await attempt(f"HF Models ({tag})", hf_models(tag))
    if cfg.github:
        for query in cfg.github_queries:
            await attempt(f"GitHub ({query})", github(query))
    for f in cfg.feeds:
        await attempt(f"RSS {f.name}", feed(f))

    unique: dict[str, Found] = {}
    for item in found:
        unique.setdefault(item.url, item)
    return list(unique.values()), errors
