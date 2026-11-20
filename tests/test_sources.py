from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import db
from app.models import ItemStatus, SourceItem
from app.sources import service
from app.sources.collectors import (
    Feed,
    Found,
    load_sources_config,
    parse_dt,
    parse_feed,
    parse_github,
    parse_hf_models,
    parse_hf_papers,
)
from app.sources.http import clean_markdown, html_to_text

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def test_parse_dt_formats():
    assert parse_dt("2026-11-16T00:00:00.000Z") == datetime(2026, 11, 16, tzinfo=timezone.utc)
    assert parse_dt("Wed, 18 Nov 2026 21:35:43 GMT") == datetime(2026, 11, 18, 21, 35, 43, tzinfo=timezone.utc)
    assert parse_dt("2026-11-20T16:30:48+00:00").hour == 16
    assert parse_dt("вчера") is None and parse_dt(None) is None


def test_parse_hf_papers():
    data = [
        {
            "paper": {
                "id": "2611.21885",
                "title": "Language Models Can\n Rewrite Their Own Weights",
                "summary": "Abstract  text.",
                "upvotes": 12,
                "publishedAt": "2026-11-16T00:00:00.000Z",
                "authors": [{"name": "A"}, {"name": "B"}],
            }
        },
        {"paper": {"title": "без id"}},
    ]
    (item,) = parse_hf_papers(data)
    assert item.rubric == "paper" and item.url == "https://huggingface.co/papers/2611.21885"
    assert item.title == "Language Models Can Rewrite Their Own Weights" and item.summary == "Abstract text."
    assert item.meta["arxiv_url"] == "https://arxiv.org/abs/2611.21885" and item.meta["authors"] == ["A", "B"]
    assert item.published_at == datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_parse_hf_models_filters_old_unpopular_and_private():
    def model(mid, likes, created, **extra):
        return {"id": mid, "likes": likes, "createdAt": created, "tags": ["moe", "region:us"], **extra}

    data = [
        model("lab/new-hit", 300, "2026-11-10T00:00:00.000Z", pipeline_tag="text-generation"),
        model("lab/too-old", 9000, "2025-01-01T00:00:00.000Z"),
        model("lab/unpopular", 3, "2026-11-10T00:00:00.000Z"),
        model("lab/private", 300, "2026-11-10T00:00:00.000Z", private=True),
    ]
    (item,) = parse_hf_models(data, min_likes=50, max_age_days=45, now=NOW)
    assert item.title == "lab/new-hit" and item.rubric == "release"
    assert item.url == "https://huggingface.co/lab/new-hit" and "region:us" not in item.summary


def test_parse_github_skips_forks_and_archived():
    data = {
        "items": [
            {"full_name": "a/tool", "html_url": "https://github.com/a/tool", "description": "Inference server",
             "stargazers_count": 1200, "created_at": "2026-10-25T08:09:52Z", "topics": ["llm"], "license": {"spdx_id": "MIT"}},
            {"full_name": "a/fork", "html_url": "https://github.com/a/fork", "fork": True},
            {"full_name": "a/old", "html_url": "https://github.com/a/old", "archived": True},
        ]
    }  # fmt: skip
    (item,) = parse_github(data)
    assert item.rubric == "tool" and item.meta == {
        "repo": "a/tool", "stars": 1200, "language": None, "topics": ["llm"], "license": "MIT",
    }  # fmt: skip


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
<item><title>Fresh &amp; new</title><link>https://example.com/fresh</link>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description><pubDate>Wed, 18 Nov 2026 21:35:43 GMT</pubDate></item>
<item><title>Old</title><link>https://example.com/old</link><pubDate>Wed, 18 Nov 2020 21:35:43 GMT</pubDate></item>
<item><title>No link</title><link></link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom post</title><link href="https://example.com/self" rel="self"/>
<link href="https://example.com/atom" rel="alternate"/><summary>Summary here</summary>
<published>2026-11-20T16:30:48+00:00</published></entry></feed>"""


def test_parse_rss_and_atom():
    feed = Feed("Blog", "https://example.com/rss", "release")
    (item,) = parse_feed(RSS, feed, limit=20, max_age_days=14, now=NOW)
    assert (item.title, item.url, item.summary) == ("Fresh & new", "https://example.com/fresh", "Hello world")
    assert item.source == "rss:Blog" and item.rubric == "release"
    (atom,) = parse_feed(ATOM, feed, limit=20, max_age_days=14, now=NOW)
    assert (atom.title, atom.url, atom.summary) == ("Atom post", "https://example.com/atom", "Summary here")
    assert parse_feed(RSS, feed, limit=0, max_age_days=14, now=NOW) == []


def test_text_cleanup():
    assert html_to_text("<nav>menu</nav><p>Привет, <b>мир</b></p><script>x()</script>") == "Привет, мир"
    md = '<div align="center"><img src="logo.png"/></div>\n\n# Tool\n\n![badge](http://b)\n\n```bash\npip install <pkg>\n```'
    cleaned = clean_markdown(md)
    assert "<div" not in cleaned and "badge" not in cleaned and "# Tool" in cleaned and "pip install <pkg>" in cleaned


def test_default_sources_config_loads():
    from app.config import DATA_DIR

    cfg = load_sources_config(DATA_DIR / "sources.yaml")
    assert cfg.enabled and cfg.feeds and all(f.rubric in ("paper", "release", "tool") for f in cfg.feeds)
    assert not load_sources_config(DATA_DIR / "нет-такого.yaml").enabled


def found(n: int, rubric: str = "paper", **kw) -> Found:
    return Found(source="test", rubric=rubric, url=f"https://example.com/{rubric}/{n}", title=f"{rubric} {n}", **kw)


async def test_store_is_idempotent_and_ignores_unknown_rubrics(database):
    async with db.session() as s:
        assert await service.store_found(s, [found(1), found(2), found(1), found(3, rubric="nope")]) == 2
        assert await service.store_found(s, [found(1), found(4)]) == 1
        assert len((await s.scalars(select(SourceItem))).all()) == 3


class RankLLM:
    def __init__(self, answer: str) -> None:
        self.answer, self.calls = answer, []

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        self.calls.append((user, cheap))
        return self.answer


async def test_rank_uses_cheap_model_and_handles_missing_scores(database):
    async with db.session() as s:
        await service.store_found(s, [found(1), found(2), found(3)])
        ids = [i.id for i in (await s.scalars(select(SourceItem).order_by(SourceItem.id))).all()]
        llm = RankLLM(f'{{"items": [{{"id": {ids[0]}, "score": 9, "why": "топ"}}, {{"id": {ids[1]}, "score": 42}}]}}')
        assert await service.rank_new(s, llm) == 3
        items = (await s.scalars(select(SourceItem).order_by(SourceItem.id))).all()
        assert [(i.score, i.status) for i in items] == [(9, "ranked"), (10, "ranked"), (0, "ranked")]
        assert items[0].score_reason == "топ" and all(cheap for _, cheap in llm.calls)
        assert await service.rank_new(s, llm) == 0  # повторно не оценивает


async def test_unparseable_ranking_leaves_items_for_next_run(database):
    async with db.session() as s:
        await service.store_found(s, [found(1)])
        assert await service.rank_new(s, RankLLM("не JSON")) == 0
        assert (await s.scalars(select(SourceItem.status))).all() == [ItemStatus.NEW.value]


async def test_digest_picks_best_fresh_unseen(database, sources_cfg):
    async with db.session() as s:
        await service.store_found(s, [found(n) for n in range(1, 7)] + [found(1, rubric="tool")])
        items = (await s.scalars(select(SourceItem).order_by(SourceItem.id))).all()
        for it, score in zip(items, [9, 8, 7, 6, 5, 10, 7], strict=True):
            it.score, it.status = score, ItemStatus.RANKED.value
        items[5].fetched_at = datetime.now(timezone.utc) - timedelta(days=30)  # лучший, но устарел
        await s.commit()

        digest = await service.digest_items(s, sources_cfg, include_offered=False)
        assert [i.score for i in digest["paper"]] == [9, 8, 7] and [i.score for i in digest["tool"]] == [7]
        await service.mark_offered(s, digest["paper"])

        again = await service.digest_items(s, sources_cfg, include_offered=False)
        assert [i.score for i in again["paper"]] == [6]  # показанные и ниже порога не возвращаются
        full = await service.digest_items(s, sources_cfg, include_offered=True)
        assert [i.score for i in full["paper"]] == [9, 8, 7]
