"""Рубрика-курс: уроки по программе из data/course.yaml, ссылка на предыдущий урок и оглавление в канале."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path

import yaml
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, Settings
from app.models import Draft, DraftStatus, KeyValue, Topic, TopicStatus
from app.rubrics import get_rubric

log = logging.getLogger(__name__)

TITLE_LIMIT = 50
_NUMBER_RE = re.compile(r"^Урок (\d+)\. ")
QUIZ_PREFIX = "Проверь себя: "
_TAG_RE = re.compile(r"<[^>]+>")
LESSON_TEXT_LIMIT = 4000


@dataclass(frozen=True)
class Lesson:
    n: int
    title: str
    covers: str
    block: str

    @property
    def topic_title(self) -> str:
        return f"Урок {self.n}. {self.title}"


@dataclass(frozen=True)
class Course:
    title: str
    intro: str
    blocks: tuple[tuple[str, tuple[Lesson, ...]], ...]

    @property
    def lessons(self) -> tuple[Lesson, ...]:
        return tuple(lesson for _, lessons in self.blocks for lesson in lessons)

    def by_number(self, n: int | None) -> Lesson | None:
        return next((lesson for lesson in self.lessons if lesson.n == n), None)


def load_course(path: str | Path | None = None) -> Course:
    """Читает программу. Файл перечитывается при каждом обращении: будущие уроки можно править на ходу."""
    raw = yaml.safe_load(Path(path or DATA_DIR / "course.yaml").read_text(encoding="utf-8")) or {}
    blocks = []
    last = 0
    for block in raw.get("blocks") or []:
        lessons = []
        for item in block.get("lessons") or []:
            n, title, covers = int(item["n"]), str(item["title"]).strip(), str(item.get("covers") or "").strip()
            if n <= last:
                raise ValueError(f"course.yaml: номер урока {n} идёт после {last}, номера должны возрастать")
            if not title or len(title) > TITLE_LIMIT:
                raise ValueError(f"course.yaml: у урока {n} название пустое или длиннее {TITLE_LIMIT} символов")
            if not covers:
                raise ValueError(f"course.yaml: у урока {n} нет тезисов (covers)")
            last = n
            lessons.append(Lesson(n=n, title=title, covers=covers, block=str(block["title"]).strip()))
        blocks.append((str(block["title"]).strip(), tuple(lessons)))
    return Course(
        title=str(raw.get("title") or "").strip(), intro=str(raw.get("intro") or "").strip(), blocks=tuple(blocks)
    )


def lesson_number(topic_title: str) -> int | None:
    found = _NUMBER_RE.match(topic_title or "")
    return int(found.group(1)) if found else None


def quiz_title(lesson_topic_title: str) -> str:
    return QUIZ_PREFIX + lesson_topic_title


def quiz_lesson_title(quiz_topic_title: str) -> str:
    """Название урока, к которому относится квиз."""
    return (quiz_topic_title or "").removeprefix(QUIZ_PREFIX)


async def _lesson_draft(session: AsyncSession, lesson_rubric: str, lesson_topic_title: str) -> Draft | None:
    """Последний одобренный или вышедший черновик урока."""
    return await session.scalar(
        select(Draft)
        .join(Topic, Topic.id == Draft.topic_id)
        .where(
            Draft.rubric == lesson_rubric,
            Topic.title == lesson_topic_title,
            Draft.status.in_((DraftStatus.APPROVED.value, DraftStatus.PUBLISHED.value)),
        )
        .order_by(Draft.id.desc())
        .limit(1)
    )


async def ensure_quiz_topic(session: AsyncSession, quiz_rubric: str) -> Topic | None:
    """Заводит тему квиза для самого свежего одобренного или вышедшего урока, если её ещё нет.

    Квизы к старым урокам задним числом не готовятся: проверять себя есть смысл в день урока.
    """
    lesson_rubric = get_rubric(quiz_rubric).follows
    rows = await session.execute(
        select(Topic.title)
        .join(Draft, Draft.topic_id == Topic.id)
        .where(
            Draft.rubric == lesson_rubric,
            Draft.status.in_((DraftStatus.APPROVED.value, DraftStatus.PUBLISHED.value)),
        )
    )
    lessons = [title for (title,) in rows.all() if lesson_number(title) is not None]
    if not lessons:
        return None
    title = quiz_title(max(lessons, key=lesson_number))
    if await session.scalar(select(Topic).where(Topic.rubric == quiz_rubric, Topic.title == title)):
        return None
    topic = Topic(rubric=quiz_rubric, title=title)
    session.add(topic)
    await session.commit()
    return topic


async def quiz_prompt_block(session: AsyncSession, quiz_rubric: str, quiz_topic_title: str) -> str:
    """Данные для квиза: тезисы урока и текст поста, который прочитал читатель."""
    lesson_title = quiz_lesson_title(quiz_topic_title)
    lesson = load_course().by_number(lesson_number(lesson_title))
    draft = await _lesson_draft(session, get_rubric(quiz_rubric).follows, lesson_title)
    if lesson is None or draft is None:
        raise LookupError(f"нет одобренного урока «{lesson_title}»: квиз писать не по чему")
    text = unescape(_TAG_RE.sub("", draft.payload.get("html") or ""))[:LESSON_TEXT_LIMIT]
    return "\n".join(
        [
            "<lesson>",
            f"Урок: {lesson.topic_title}",
            "Тезисы урока:",
            lesson.covers,
            "Текст урока, который прочитал читатель:",
            text,
            "</lesson>",
        ]
    )


def quiz_question_prefix(quiz_topic_title: str) -> str:
    n = lesson_number(quiz_lesson_title(quiz_topic_title))
    return f"Урок {n}, проверь себя. " if n else ""


async def quiz_lesson_published(session: AsyncSession, quiz_draft: Draft) -> bool:
    """Вышел ли урок, к которому относится квиз: раньше урока квиз публиковать нельзя."""
    topic = await session.get(Topic, quiz_draft.topic_id) if quiz_draft.topic_id else None
    if topic is None:
        return True
    lesson_rubric = get_rubric(quiz_draft.rubric).follows
    n = lesson_number(quiz_lesson_title(topic.title))
    return n in await published(session, lesson_rubric)


async def sync(session: AsyncSession, rubric_key: str, course: Course | None = None) -> int:
    """Приводит ещё не написанные уроки в базе к программе. Возвращает число добавленных.

    Программа главнее базы: урок, которого в ней больше нет, из очереди убирается. Уроки, по которым
    уже есть черновик или пост, не трогаются.
    """
    course = course or load_course()
    wanted = {lesson.topic_title for lesson in course.lessons}
    topics = (await session.scalars(select(Topic).where(Topic.rubric == rubric_key))).all()
    known = {t.title for t in topics}
    for topic in topics:
        if topic.status == TopicStatus.NEW.value and topic.title not in wanted:
            topic.status = TopicStatus.SKIPPED.value
        elif topic.status == TopicStatus.SKIPPED.value and topic.title in wanted:
            topic.status = TopicStatus.NEW.value  # урок вернули в программу
    added = 0
    for title in wanted - known:
        session.add(Topic(rubric=rubric_key, title=title))
        added += 1
    await session.commit()
    return added


async def next_lesson_topic(session: AsyncSession, rubric_key: str) -> Topic | None:
    """Ненаписанный урок с наименьшим номером."""
    topics = (
        await session.scalars(select(Topic).where(Topic.rubric == rubric_key, Topic.status == TopicStatus.NEW.value))
    ).all()
    numbered = [(lesson_number(t.title), t) for t in topics]
    numbered = [(n, t) for n, t in numbered if n is not None]
    return min(numbered, key=lambda pair: pair[0])[1] if numbered else None


def prompt_block(course: Course, lesson: Lesson) -> str:
    """Данные урока для модели: тезисы и место урока в программе."""
    done = [x.topic_title for x in course.lessons if x.n < lesson.n]
    ahead = [x.topic_title for x in course.lessons if x.n > lesson.n]
    lines = [
        "<lesson>",
        f"Блок курса: {lesson.block}",
        "Нужно раскрыть:",
        lesson.covers,
        "Уже пройдено: " + ("; ".join(done) if done else "ничего, это первый урок"),
        "Следующий урок: " + (ahead[0] if ahead else "нет, это последний урок курса"),
    ]
    if ahead[1:]:
        lines.append("Впереди: " + "; ".join(ahead[1:]))
    lines.append("</lesson>")
    return "\n".join(lines)


def post_url(settings: Settings, message_id: int) -> str | None:
    """Адрес поста в публичном канале. У канала без @имени постоянной публичной ссылки нет."""
    channel = settings.channel_id.strip()
    return f"https://t.me/{channel[1:]}/{message_id}" if channel.startswith("@") else None


async def published(session: AsyncSession, rubric_key: str) -> dict[int, int]:
    """Вышедшие уроки: номер урока → id сообщения в канале."""
    rows = await session.execute(
        select(Topic.title, Draft.channel_message_id)
        .join(Draft, Draft.topic_id == Topic.id)
        .where(
            Draft.rubric == rubric_key,
            Draft.status == DraftStatus.PUBLISHED.value,
            Draft.channel_message_id.is_not(None),
        )
    )
    return {n: message_id for title, message_id in rows.all() if (n := lesson_number(title)) is not None}


async def nav_html(session: AsyncSession, settings: Settings, rubric_key: str, topic_title: str) -> tuple[str, str]:
    """Строка со ссылкой на предыдущий урок и замечание для автора, если ссылку поставить не на что."""
    n = lesson_number(topic_title)
    previous = load_course().by_number(n - 1) if n and n > 1 else None
    if previous is None:
        return "", ""
    message_id = (await published(session, rubric_key)).get(previous.n)
    url = post_url(settings, message_id) if message_id else None
    if not url:
        return "", f"в посте нет ссылки на предыдущий урок: «{previous.topic_title}» ещё не опубликован"
    return f'Предыдущий урок: <a href="{escape(url)}">{escape(previous.topic_title)}</a>', ""


def toc_html(course: Course, links: dict[int, str], *, brand_emoji: str = "", hashtag: str = "") -> str:
    """Оглавление курса: вышедшие уроки — ссылками, остальные — обычным текстом."""
    head = f"{brand_emoji} {course.title}: все уроки".strip()
    parts = [f"<b>{escape(head)}</b>"]
    if course.intro:
        parts.append(escape(course.intro))
    for title, lessons in course.blocks:
        rows = [f"<b>{escape(title)}</b>"]
        for lesson in lessons:
            name = escape(lesson.title)
            link = links.get(lesson.n)
            rows.append(f"{lesson.n}. " + (f'<a href="{escape(link)}">{name}</a>' if link else name))
        parts.append("\n".join(rows))
    if hashtag:
        parts.append(hashtag)
    return "\n\n".join(parts)


async def update_toc(session: AsyncSession, bot: Bot, settings: Settings, rubric_key: str, chat_id: int | str) -> str:
    """Обновляет пост-оглавление в канале; если его ещё нет (или его удалили) — публикует и закрепляет.

    Возвращает замечание для автора (пустая строка — всё в порядке).
    """
    links = {
        n: url
        for n, message_id in (await published(session, rubric_key)).items()
        if (url := post_url(settings, message_id))
    }
    if not links:
        return ""  # канал без публичного имени: ссылаться не на что, оглавление не ведём
    html = toc_html(load_course(), links, brand_emoji=settings.brand_emoji, hashtag=get_rubric(rubric_key).hashtag)
    no_preview = LinkPreviewOptions(is_disabled=True)
    key = f"toc:{rubric_key}"
    stored = await session.get(KeyValue, key)
    if stored:
        try:
            await bot.edit_message_text(
                text=html,
                chat_id=chat_id,
                message_id=int(stored.value),
                parse_mode="HTML",
                link_preview_options=no_preview,
            )
            return ""
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return ""
            log.warning("оглавление курса не обновилось (%s), публикую заново", e)
    message = await bot.send_message(chat_id=chat_id, text=html, parse_mode="HTML", link_preview_options=no_preview)
    if stored:
        stored.value = str(message.message_id)
    else:
        session.add(KeyValue(key=key, value=str(message.message_id)))
    await session.commit()
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
    except TelegramAPIError as e:
        return f"Оглавление курса опубликовано, но закрепить его не получилось ({e}). Закрепи пост вручную."
    return ""
