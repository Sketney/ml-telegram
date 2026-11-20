from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.config import DATA_DIR, Settings
from app.scheduler import build_scheduler, load_schedule
from app.sources.collectors import load_sources_config


def test_default_schedule_loads():
    schedule = load_schedule(DATA_DIR / "schedule.yaml")
    assert schedule.buffer >= 1 and schedule.slots
    assert set(schedule.rubrics) <= {"concept", "history", "quiz", "paper", "tool", "release"}


def test_slot_with_several_rubrics(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text(
        "slots:\n  - {day: wed, time: '10:00', rubrics: [tool, paper]}\n  - {day: mon, time: '09:30', rubric: quiz}",
        encoding="utf-8",
    )
    schedule = load_schedule(path)
    assert [s.rubrics for s in schedule.slots] == [("tool", "paper"), ("quiz",)]
    assert schedule.rubrics == ["tool", "paper", "quiz"]
    path.write_text("slots:\n  - {day: wed, time: '10:00'}", encoding="utf-8")
    with pytest.raises(ValueError, match="не указана рубрика"):
        load_schedule(path)


@pytest.mark.parametrize(
    "body, needle",
    [
        ("slots:\n  - {day: funday, time: '10:00', rubric: quiz}", "день"),
        ("slots:\n  - {day: mon, time: '25:00', rubric: quiz}", "время"),
        ("prepare_at: 'вечером'\nslots: []", "время"),
    ],
)
def test_bad_schedule_is_reported(tmp_path, body, needle):
    path = tmp_path / "schedule.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=needle):
        load_schedule(path)


def test_unknown_rubric_in_schedule(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text("slots:\n  - {day: mon, time: '10:00', rubric: nope}", encoding="utf-8")
    with pytest.raises(KeyError, match="nope"):
        load_schedule(path)


async def test_jobs_fire_in_channel_timezone():
    settings = Settings(_env_file=None, admin_id=1, timezone="Asia/Tbilisi")
    schedule = load_schedule(DATA_DIR / "schedule.yaml")
    sources_cfg = load_sources_config(DATA_DIR / "sources.yaml")
    scheduler = build_scheduler(bot=None, settings=settings, llm=None, schedule=schedule, sources_cfg=sources_cfg)
    scheduler.start(paused=True)
    try:
        jobs = {j.id: j for j in scheduler.get_jobs()}
        assert len(jobs) == 2 + len(schedule.slots)
        collect = jobs["sources"].next_run_time.astimezone(ZoneInfo("Asia/Tbilisi"))
        assert (collect.hour, collect.minute) == (sources_cfg.collect_hour, sources_cfg.collect_minute)
        tz = ZoneInfo("Asia/Tbilisi")
        prep = jobs["prepare"].next_run_time.astimezone(tz)
        assert (prep.hour, prep.minute) == (schedule.prepare_hour, schedule.prepare_minute)
        for i, slot in enumerate(schedule.slots):
            run = jobs[f"publish-{i}-{slot.day}-{'+'.join(slot.rubrics)}"].next_run_time.astimezone(tz)
            assert (run.strftime("%a").lower(), run.hour, run.minute) == (slot.day, slot.hour, slot.minute)
    finally:
        scheduler.shutdown(wait=False)
