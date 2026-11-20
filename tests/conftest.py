from __future__ import annotations

import asyncio
import contextlib

import pytest

from app import db, generator
from app.config import Settings
from app.llm import FakeLLM
from app.main import make_bot, make_dispatcher
from tests.mock_telegram import MockTelegram

ADMIN = 4242
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
async def tg(settings, database, llm):
    """Запущенный бот, подключённый к мок-серверу Telegram."""
    mock = MockTelegram()
    await mock.start()
    settings.telegram_api_base = mock.base
    bot = make_bot(settings)
    dp = make_dispatcher(settings, llm)
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False, polling_timeout=1))
    mock.bot = bot
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
