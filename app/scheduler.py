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

from app import db, generator, moderation
from app.bot.cards import send_card
from app.config import Settings
from app.llm import LLM
from app.rubrics import get_rubric

log = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MISFIRE_GRACE_SECONDS = 3600  # бот был недоступен меньше часа — задача всё равно выполнится


@dataclass(frozen=True)
class Slot:
    day: str
    hour: int
    minute: int
    rubric: str


@dataclass(frozen=True)
class Schedule:
    prepare_hour: int
    prepare_minute: int
    buffer: int
    slots: tuple[Slot, ...]

    @property
    def rubrics(self) -> list[str]:
        return list(dict.fromkeys(s.rubric for s in self.slots))


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
        get_rubric(item["rubric"])  # KeyError с понятным текстом, если рубрики нет
        h, m = _hm(item.get("time"), f"слот {i}")
        slots.append(Slot(day=day, hour=h, minute=m, rubric=item["rubric"]))
    return Schedule(prepare_hour=ph, prepare_minute=pm, buffer=max(1, int(raw.get("buffer", 1))), slots=tuple(slots))


async def _notify(bot: Bot, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(settings.admin_id, text)
    except Exception:
        log.exception("не удалось написать автору: %s", text)


async def prepare_job(bot: Bot, settings: Settings, llm: LLM, schedule: Schedule) -> None:
    """Для каждой рубрики из расписания держит buffer черновиков в работе."""
    for rubric_key in schedule.rubrics:
        title = get_rubric(rubric_key).title
        async with db.session() as s:
            try:
                missing = schedule.buffer - await moderation.count_in_work(s, rubric_key)
                for _ in range(max(0, missing)):
                    draft = await generator.generate_draft(s, llm, settings, rubric_key)
                    await send_card(bot, settings.admin_id, s, draft, note="Плановый черновик к ближайшему слоту.")
            except generator.NoTopics:
                await _notify(bot, settings, f"«{title}»: в бэклоге закончились темы. Добавь: /add {rubric_key} тема")
            except Exception as e:
                log.exception("подготовка черновика %s не удалась", rubric_key)
                await _notify(bot, settings, f"«{title}»: не получилось подготовить черновик: {e}")


async def publish_slot_job(bot: Bot, settings: Settings, rubric_key: str) -> None:
    """Публикует самый старый одобренный черновик рубрики."""
    title = get_rubric(rubric_key).title
    async with db.session() as s:
        draft = await moderation.next_approved(s, rubric_key)
        if draft is None:
            await _notify(bot, settings, f"Слот «{title}» пропущен: нет одобренного черновика. /queue — что в работе.")
            return
        try:
            await moderation.publish(s, bot, settings, draft)
        except Exception as e:
            log.exception("публикация черновика %s не удалась", draft.id)
            await _notify(bot, settings, f"Не получилось опубликовать черновик #{draft.id} («{title}»): {e}")
            return
    await _notify(bot, settings, f"Опубликован черновик #{draft.id} («{title}») по расписанию.")


def build_scheduler(bot: Bot, settings: Settings, llm: LLM, schedule: Schedule) -> AsyncIOScheduler:
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
            args=(bot, settings, slot.rubric),
            id=f"publish-{i}-{slot.day}-{slot.rubric}",
            **common,
        )
    return scheduler
