from __future__ import annotations

import asyncio
import contextlib

import pytest

from app import db, generator, moderation, render
from app import transcribe as stt
from app.config import Settings
from app.llm import FakeLLM
from app.main import make_bot, make_dispatcher
from app.sources import service as sources_service
from app.sources.collectors import SourcesConfig
from tests.mock_telegram import MockTelegram

ADMIN = 4242
FAKE_PNG = b"\x89PNG\r\n\x1a\n-fake-cheatsheet"
CHANNEL = "@test_channel"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:TEST",
        channel_id=CHANNEL,
        admin_id=ADMIN,
        llm_provider="fake",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        brand_emoji="🔹",
        channel_signature="",
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Тесты не ходят в сеть: текст источника и сбор подменяются."""

    async def fake_grounding(item):
        return f"ТЕКСТ ИСТОЧНИКА: {item.title}. " + "Подробности из источника. " * 20, "тестовый текст"

    async def fake_collect(cfg, now=None):
        raise AssertionError("тест должен подменить сбор источников сам")

    async def fake_render(sheet, **kwargs):
        return FAKE_PNG

    async def fake_transcribe(data, settings):
        return "Расшифровка голосового: " + "пришли разработчики и сказали, что балансировка не работает. " * 3

    monkeypatch.setattr(sources_service, "grounding_for", fake_grounding)
    monkeypatch.setattr(sources_service, "collect", fake_collect)
    render.__dict__.setdefault("render_sheet_real", render.render_sheet)
    monkeypatch.setattr(render, "render_sheet", fake_render)
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    moderation.set_slotted_rubrics(None)
    yield
    moderation.set_slotted_rubrics(None)


@pytest.fixture
def sources_cfg() -> SourcesConfig:
    return SourcesConfig(digest_top=3, min_score=6, max_age_days=14)


@pytest.fixture
async def database(settings):
    db.init_engine(settings.database_url)
    await db.create_schema()
    async with db.session() as s:
        await generator.sync_backlog(s)
    yield
    await db.dispose()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def tg(settings, database, llm, sources_cfg):
    """Запущенный бот, подключённый к мок-серверу Telegram."""
    mock = MockTelegram()
    await mock.start()
    settings.telegram_api_base = mock.base
    bot = make_bot(settings)
    dp = make_dispatcher(settings, llm, sources_cfg)
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False, polling_timeout=1))
    mock.bot, mock.llm, mock.cfg, mock.settings = bot, llm, sources_cfg, settings
    try:
        yield mock
    finally:
        with contextlib.suppress(Exception):
            await dp.stop_polling()
        polling.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await polling
        await bot.session.close()
        await mock.stop()
