"""Расписание: ежедневная подготовка черновиков и публикация одобренных по слотам."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import course, db, generator, metrics, moderation
from app.bot.cards import send_card
from app.bot.digest import send_digest
from app.config import Settings
from app.llm import LLM
from app.rubrics import get_rubric
from app.sources import service as sources
from app.sources.collectors import SourcesConfig

log = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MISFIRE_GRACE_SECONDS = 3600  # бот был недоступен меньше часа — задача всё равно выполнится


@dataclass(frozen=True)
class Slot:
    day: str
    hour: int
    minute: int
    rubrics: tuple[str, ...]  # публикуется самый давно одобренный черновик любой из этих рубрик


@dataclass(frozen=True)
class Schedule:
    prepare_hour: int
    prepare_minute: int
    buffer: int
    slots: tuple[Slot, ...]
    stats_hour: int = 9
    stats_minute: int = 0

    @property
    def rubrics(self) -> list[str]:
        return list(dict.fromkeys(r for s in self.slots for r in s.rubrics))


def _hm(value: str, where: str) -> tuple[int, int]:
    try:
        h, m = str(value).split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except ValueError:
        raise ValueError(f"schedule.yaml: {where} — время «{value}», ожидается ЧЧ:ММ") from None


def load_schedule(path: str | Path) -> Schedule:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    ph, pm = _hm(raw.get("prepare_at", "18:00"), "prepare_at")
    slots = []
    for i, item in enumerate(raw.get("slots") or [], 1):
        day = str(item.get("day", "")).lower()
        if day not in DAYS:
            raise ValueError(f"schedule.yaml: слот {i} — день «{day}», ожидается один из {', '.join(DAYS)}")
        rubrics = item.get("rubrics") or [item.get("rubric")]
        if isinstance(rubrics, str):
            rubrics = [rubrics]
        if not rubrics or not all(rubrics):
            raise ValueError(f"schedule.yaml: слот {i} — не указана рубрика (rubric или rubrics)")
        for key in rubrics:
            get_rubric(key)  # KeyError с понятным текстом, если рубрики нет
        h, m = _hm(item.get("time"), f"слот {i}")
        slots.append(Slot(day=day, hour=h, minute=m, rubrics=tuple(rubrics)))
    sh, sm = _hm(raw.get("stats_at", "09:00"), "stats_at")
    return Schedule(
        prepare_hour=ph,
        prepare_minute=pm,
        buffer=max(1, int(raw.get("buffer", 1))),
        slots=tuple(slots),
        stats_hour=sh,
        stats_minute=sm,
    )


async def _notify(bot: Bot, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(settings.admin_id, text)
    except Exception:
        log.exception("не удалось написать автору: %s", text)


async def prepare_job(bot: Bot, settings: Settings, llm: LLM, schedule: Schedule) -> None:
    """Для каждой рубрики по бэклогу из расписания держит buffer черновиков в работе."""
    for rubric_key in schedule.rubrics:
        rubric = get_rubric(rubric_key)
        if rubric.source not in ("backlog", "lesson"):
            continue  # рубрики по источникам автор запускает сам из дайджеста
        title = rubric.title
        async with db.session() as s:
            try:
                if rubric.follows:  # квиз к уроку: тема появляется, когда урок одобрен
                    await course.ensure_quiz_topic(s, rubric_key)
                missing = schedule.buffer - await moderation.count_in_work(s, rubric_key)
                for _ in range(max(0, missing)):
                    draft = await generator.generate_draft(s, llm, settings, rubric_key)
                    await send_card(bot, settings.admin_id, s, draft, note="Плановый черновик к ближайшему слоту.")
            except generator.NoTopics:
                if rubric.series or rubric.follows:
                    continue  # курс дописан или урок ещё не одобрен: напоминать об этом каждый день незачем
                await _notify(bot, settings, f"«{title}»: в бэклоге закончились темы. Добавь: /add {rubric_key} тема")
            except Exception as e:
                log.exception("подготовка черновика %s не удалась", rubric_key)
                await _notify(bot, settings, f"«{title}»: не получилось подготовить черновик: {e}")


async def publish_slot_job(bot: Bot, settings: Settings, rubrics: str | tuple[str, ...]) -> None:
    """Публикует самый давно одобренный черновик из рубрик слота."""
    keys = (rubrics,) if isinstance(rubrics, str) else tuple(rubrics)
    title = " / ".join(get_rubric(k).title for k in keys)
    async with db.session() as s:
        draft = await moderation.next_approved(s, keys)
        if draft is None:
            await _notify(bot, settings, f"Слот «{title}» пропущен: нет одобренного черновика. /queue — что в работе.")
            return
        try:
            await moderation.publish(s, bot, settings, draft)
        except Exception as e:
            log.exception("публикация черновика %s не удалась", draft.id)
            await _notify(bot, settings, f"Не получилось опубликовать черновик #{draft.id} («{title}»): {e}")
            return
    await _notify(
        bot, settings, f"Опубликован черновик #{draft.id} («{get_rubric(draft.rubric).title}») по расписанию."
    )


async def stats_job(settings: Settings) -> None:
    """Снимает просмотры опубликованных постов. Тихая задача: о сбоях пишет только в лог."""
    async with db.session() as s:
        try:
            updated = await metrics.refresh(s, settings)
            log.info("статистика: обновлено постов — %s", updated)
        except metrics.StatsUnavailable as e:
            log.warning("статистика недоступна: %s", e)
        except Exception:
            log.exception("сбор статистики не удался")


async def sources_job(bot: Bot, settings: Settings, llm: LLM, cfg: SourcesConfig) -> None:
    """Собирает источники, оценивает новое и присылает автору дайджест кандидатов."""
    async with db.session() as s:
        try:
            report = await sources.collect_and_rank(s, llm, cfg)
        except Exception as e:
            log.exception("сбор источников не удался")
            await _notify(bot, settings, f"Не получилось собрать источники: {e}")
            return
        if report.errors:
            await _notify(bot, settings, "Источники не ответили:\n" + "\n".join(f"• {e}" for e in report.errors)[:3500])
        try:
            await send_digest(bot, settings.admin_id, s, cfg, include_offered=False)
        except Exception:
            log.exception("не удалось отправить дайджест")


def build_scheduler(
    bot: Bot, settings: Settings, llm: LLM, schedule: Schedule, sources_cfg: SourcesConfig | None = None
) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)
    common = {"misfire_grace_time": MISFIRE_GRACE_SECONDS, "coalesce": True, "max_instances": 1}
    scheduler.add_job(
        prepare_job,
        CronTrigger(hour=schedule.prepare_hour, minute=schedule.prepare_minute, timezone=tz),
        args=(bot, settings, llm, schedule),
        id="prepare",
        **common,
    )
    for i, slot in enumerate(schedule.slots):
        scheduler.add_job(
            publish_slot_job,
            CronTrigger(day_of_week=slot.day, hour=slot.hour, minute=slot.minute, timezone=tz),
            args=(bot, settings, slot.rubrics),
            id=f"publish-{i}-{slot.day}-{'+'.join(slot.rubrics)}",
            **common,
        )
    scheduler.add_job(
        stats_job,
        CronTrigger(hour=schedule.stats_hour, minute=schedule.stats_minute, timezone=tz),
        args=(settings,),
        id="stats",
        **common,
    )
    if sources_cfg is not None and sources_cfg.enabled:
        scheduler.add_job(
            sources_job,
            CronTrigger(hour=sources_cfg.collect_hour, minute=sources_cfg.collect_minute, timezone=tz),
            args=(bot, settings, llm, sources_cfg),
            id="sources",
            **common,
        )
    return scheduler
