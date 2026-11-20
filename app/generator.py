"""Бэклог тем и генерация черновиков."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, Settings
from app.llm import LLM
from app.models import Draft, DraftStatus, Topic, TopicStatus
from app.rubrics import Rubric, build_system_prompt, get_rubric, load_rubrics
from app.validator import validate_payload

log = logging.getLogger(__name__)

SHOW_ANSWER_OPTION = "Посмотреть ответ"


class NoTopics(Exception):
    """В бэклоге рубрики не осталось новых тем."""


class GenerationFailed(Exception):
    """Модель не вернула разборчивый ответ."""


# --- бэклог ---------------------------------------------------------------


async def sync_backlog(session: AsyncSession, path: Path | None = None) -> int:
    """Добавляет в базу темы из backlog.yaml, которых там ещё нет. Возвращает число новых."""
    path = path or DATA_DIR / "backlog.yaml"
    if not path.exists():
        return 0
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = load_rubrics()
    added = 0
    for rubric, items in raw.items():
        if rubric not in known:
            log.warning("backlog.yaml: рубрика %s не описана в rubrics.yaml, пропускаю", rubric)
            continue
        existing = set((await session.scalars(select(Topic.title).where(Topic.rubric == rubric))).all())
        for item in items or []:
            title, notes = (item, "") if isinstance(item, str) else (item["title"], item.get("notes", ""))
            title = title.strip()
            if title and title not in existing:
                session.add(Topic(rubric=rubric, title=title, notes=notes))
                existing.add(title)
                added += 1
    await session.commit()
    return added


async def add_topic(session: AsyncSession, rubric: str, title: str, notes: str = "") -> Topic | None:
    """None — такая тема в этой рубрике уже есть."""
    get_rubric(rubric)
    title = title.strip()
    dup = await session.scalar(select(Topic).where(Topic.rubric == rubric, Topic.title == title))
    if dup:
        return None
    topic = Topic(rubric=rubric, title=title, notes=notes)
    session.add(topic)
    await session.commit()
    return topic


async def next_topic(session: AsyncSession, rubric: str) -> Topic:
    topic = await session.scalar(
        select(Topic).where(Topic.rubric == rubric, Topic.status == TopicStatus.NEW.value).order_by(Topic.id).limit(1)
    )
    if topic is None:
        raise NoTopics(rubric)
    return topic


async def backlog_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Topic.rubric, func.count()).where(Topic.status == TopicStatus.NEW.value).group_by(Topic.rubric)
    )
    counts = dict(rows.all())
    return {key: counts.get(key, 0) for key in load_rubrics()}


# --- генерация ------------------------------------------------------------


def parse_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели, даже если она обернула его в ``` или добавила пояснение."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise GenerationFailed("в ответе модели нет JSON-объекта")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise GenerationFailed(f"ответ модели — не валидный JSON: {e}") from e
    if not isinstance(data, dict):
        raise GenerationFailed("ответ модели — не JSON-объект")
    return data


def compose_post_html(body: str, rubric: Rubric, settings: Settings) -> str:
    """Тело поста от модели + хештег рубрики + подпись канала."""
    parts = [body.strip().replace("{E}", settings.brand_emoji)]
    tail = "\n".join(x for x in (rubric.hashtag, settings.channel_signature.strip()) if x)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


def build_payload(data: dict, rubric: Rubric, settings: Settings) -> dict:
    if rubric.kind == "quiz":
        options = data.get("options")
        if isinstance(options, list):
            options = [str(o).strip() for o in options]
            if settings.quiz_show_answer_option and SHOW_ANSWER_OPTION not in options:
                options.append(SHOW_ANSWER_OPTION)
        return {
            "kind": "quiz",
            "question": str(data.get("question") or "").strip(),
            "options": options,
            "correct_index": data.get("correct_index"),
            "explanation": str(data.get("explanation") or "").strip(),
        }
    body = data.get("html")
    if not isinstance(body, str) or not body.strip():
        raise GenerationFailed("в ответе модели нет поля html")
    return {"kind": "post", "html": compose_post_html(body, rubric, settings)}


def build_user_prompt(topic_title: str, notes: str = "", comment: str = "", previous: dict | None = None) -> str:
    lines = [f"Тема: {topic_title}"]
    if notes:
        lines.append(f"Пожелания автора к теме: {notes}")
    if previous:
        lines.append("\nПредыдущая версия черновика, которую автор попросил переделать:")
        lines.append(json.dumps(previous, ensure_ascii=False))
    if comment:
        lines.append(f"\nКомментарий автора — учти его в новой версии: {comment}")
    return "\n".join(lines)


async def _generate_payload(llm: LLM, settings: Settings, rubric: Rubric, user_prompt: str) -> tuple[dict, list[dict]]:
    """Один вызов модели + одна попытка исправления, если валидатор нашёл ошибки."""
    system = build_system_prompt(rubric.key, settings.brand_emoji)
    payload: dict | None = None
    issues: list[dict] = []
    prompt = user_prompt
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = await llm.complete(system, prompt)
            payload = build_payload(parse_json_object(raw), rubric, settings)
        except GenerationFailed as e:
            last_error = e
            log.warning("рубрика %s, попытка %s: %s", rubric.key, attempt, e)
            prompt = f"{user_prompt}\n\nПрошлый ответ не удалось разобрать ({e}). Верни только JSON в заданном формате."
            continue
        issues = validate_payload(payload, min_len=rubric.min_len, max_len=rubric.max_len)
        errors = [i["text"] for i in issues if i["level"] == "error"]
        if not errors:
            break
        prompt = f"{user_prompt}\n\nВ прошлой версии были ошибки, исправь их и верни ответ целиком:\n- " + "\n- ".join(
            errors
        )
    if payload is None:
        raise GenerationFailed(str(last_error))
    return payload, issues


async def generate_draft(
    session: AsyncSession, llm: LLM, settings: Settings, rubric_key: str, topic: Topic | None = None
) -> Draft:
    """Берёт следующую тему из бэклога (или переданную) и создаёт черновик на модерацию."""
    rubric = get_rubric(rubric_key)
    topic = topic or await next_topic(session, rubric_key)
    payload, issues = await _generate_payload(llm, settings, rubric, build_user_prompt(topic.title, topic.notes))
    draft = Draft(rubric=rubric_key, topic_id=topic.id, payload=payload, issues=issues)
    topic.status = TopicStatus.USED.value
    session.add(draft)
    await session.commit()
    return draft


async def regenerate_draft(
    session: AsyncSession, llm: LLM, settings: Settings, draft: Draft, comment: str = ""
) -> Draft:
    """Новая версия того же черновика; с комментарием модель видит прошлую версию."""
    rubric = get_rubric(draft.rubric)
    topic = await session.get(Topic, draft.topic_id) if draft.topic_id else None
    title = topic.title if topic else "без темы"
    notes = topic.notes if topic else ""
    previous = None
    if comment:
        previous = {k: v for k, v in draft.payload.items() if k != "kind"}
        if isinstance(previous.get("options"), list):  # служебный вариант модели не показываем
            previous["options"] = [o for o in previous["options"] if o != SHOW_ANSWER_OPTION]
    payload, issues = await _generate_payload(llm, settings, rubric, build_user_prompt(title, notes, comment, previous))
    draft.payload = payload
    draft.issues = issues
    draft.version += 1
    draft.status = DraftStatus.PENDING.value
    await session.commit()
    return draft
