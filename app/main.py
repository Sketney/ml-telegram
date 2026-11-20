from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app import covers, db, generator, moderation
from app.bot.handlers import build_router
from app.config import Settings, get_settings
from app.llm import LLM, make_llm
from app.scheduler import build_scheduler, load_schedule
from app.sources.collectors import SourcesConfig, load_sources_config

log = logging.getLogger("app")

COMMANDS = [
    BotCommand(command="gen", description="Черновик: /gen рубрика [тема]"),
    BotCommand(command="queue", description="Черновики в работе"),
    BotCommand(command="digest", description="Кандидаты из источников"),
    BotCommand(command="fetch", description="Собрать источники сейчас"),
    BotCommand(command="note", description="Пост по заметкам: /note рубрика"),
    BotCommand(command="done", description="Закончить заметки и готовить черновик"),
    BotCommand(command="stats", description="Просмотры по рубрикам"),
    BotCommand(command="topics", description="Сколько тем в бэклоге"),
    BotCommand(command="add", description="Добавить тему: /add рубрика тема"),
    BotCommand(command="show", description="Показать черновик: /show номер"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="help", description="Справка"),
]


def make_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_base:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base))
    return Bot(token=settings.bot_token, session=session)


def make_dispatcher(settings: Settings, llm: LLM, sources_cfg: SourcesConfig | None = None) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["settings"] = settings
    dp["llm"] = llm
    dp["sources_cfg"] = sources_cfg or load_sources_config(settings.sources_file)
    dp.include_router(build_router(settings.admin_id))
    return dp


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    problems = settings.problems()
    if problems:
        print("Не могу запуститься, проверь .env:", *[f"  - {p}" for p in problems], sep="\n", file=sys.stderr)
        sys.exit(2)

    schedule = load_schedule(settings.schedule_file)
    sources_cfg = load_sources_config(settings.sources_file)
    moderation.set_slotted_rubrics(set(schedule.rubrics))
    covers.configure(settings)
    if not settings.media_base_url:
        log.warning("MEDIA_BASE_URL не задан: обложку получат только короткие посты (фото с подписью)")
    db.init_engine(settings.database_url)
    await db.create_schema()
    async with db.session() as s:
        added = await generator.sync_backlog(s)
    log.info("бэклог: добавлено новых тем — %s", added)

    llm = make_llm(settings)
    if settings.llm_provider == "fake":
        log.warning("LLM_PROVIDER=fake: черновики — заглушки, в канал их публиковать не нужно")

    bot = make_bot(settings)
    dp = make_dispatcher(settings, llm, sources_cfg)
    scheduler = build_scheduler(bot, settings, llm, schedule, sources_cfg)
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("расписание: %s → %s", job.id, job.next_run_time)

    await bot.set_my_commands(COMMANDS)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.dispose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
