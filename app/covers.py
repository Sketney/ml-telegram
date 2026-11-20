"""Обложки постов: картинка с рубрикой и заголовком.

Обложка — чистая функция от текста и оформления, поэтому в базе не хранится: файл лежит в MEDIA_DIR
под именем-хешем и при пропаже собирается заново. Короткий пост выходит как фото с подписью; длинному
(подпись к фото ограничена 1024 символами) картинка подставляется через предпросмотр ссылки, для этого
папку MEDIA_DIR раздаёт веб-сервер по адресу MEDIA_BASE_URL.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path

from app import render
from app.config import Settings
from app.models import Draft
from app.rubrics import get_rubric

log = logging.getLogger(__name__)

COVER_TEXT_LIMIT = 70
_FIRST_BOLD_RE = re.compile(r"<(b|strong)>(.*?)</\1>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# «Понятие недели: …», «Разбор: …» — ярлык рубрики в начале заголовка; на обложке рубрика уже подписана
_LABEL_RE = re.compile(r"^[^:]{1,30}:\s+(?=\S)")
_DID_YOU_KNOW_RE = re.compile(r"^а\s+знали\s+ли\s+вы,?\s+что\s+", re.IGNORECASE)

# Настройки задаются при запуске бота. None — обложки выключены (тесты, скрипты).
_settings: Settings | None = None


@dataclass(frozen=True)
class Cover:
    image: bytes
    url: str | None  # публичный адрес картинки; None — раздача не настроена


def configure(settings: Settings | None) -> None:
    global _settings
    _settings = settings


def cover_text(payload: dict, brand_emoji: str = "") -> str:
    """Надпись для обложки: поле cover от модели, иначе заголовок поста (первая жирная строка)."""
    text = str(payload.get("cover") or "").strip()
    if not text:
        found = _FIRST_BOLD_RE.search(payload.get("html") or "")
        text = unescape(_TAG_RE.sub("", found.group(2))) if found else ""
        text = _LABEL_RE.sub("", text.strip(), count=1)
        if _DID_YOU_KNOW_RE.match(text):
            text = _DID_YOU_KNOW_RE.sub("", text).rstrip("?")
        text = text[:1].upper() + text[1:]
    if brand_emoji:
        text = text.replace(brand_emoji, "")
    text = " ".join(text.split()).strip(" .")
    if len(text) > COVER_TEXT_LIMIT:
        text = text[:COVER_TEXT_LIMIT].rsplit(" ", 1)[0].rstrip(" ,:;—-") + "…"
    return text


def _channel(settings: Settings) -> str:
    return settings.channel_id if settings.channel_id.startswith("@") else ""


def file_name(settings: Settings, kicker: str, text: str) -> str:
    """Имя зависит от всего, что видно на обложке: изменился текст или оформление — новый адрес.

    Telegram запоминает предпросмотр по адресу, поэтому переиспользовать имя для другой картинки нельзя.
    """
    mascot = Path(settings.brand_dir) / "mascot.png"
    stamp = str(mascot.stat().st_mtime_ns) if mascot.is_file() else ""
    key = "\x1f".join([str(render.COVER_VERSION), kicker, text, _channel(settings), settings.channel_tagline, stamp])
    return hashlib.sha256(key.encode()).hexdigest()[:20] + ".jpg"


async def for_draft(draft: Draft) -> Cover | None:
    """Обложка черновика или None: рубрика без обложек, нет заголовка, не настроено или рендер не удался.

    Обложка — украшение: её отсутствие публикацию не блокирует.
    """
    settings = _settings
    if settings is None or draft.payload.get("kind") != "post":
        return None
    rubric = get_rubric(draft.rubric)
    if not rubric.cover:
        return None
    text = cover_text(draft.payload, settings.brand_emoji)
    if not text:
        return None
    name = file_name(settings, rubric.title, text)
    path = Path(settings.media_dir) / name
    try:
        if path.is_file():
            image = path.read_bytes()
        else:
            image = await render.render_cover(
                text,
                kicker=rubric.title,
                channel=_channel(settings),
                tagline=settings.channel_tagline,
                brand_dir=settings.brand_dir,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(image)
            tmp.chmod(0o644)  # файл читает веб-сервер из другого контейнера
            tmp.replace(path)  # веб-сервер не должен отдать наполовину записанный файл
    except (render.RenderUnavailable, OSError) as e:
        log.warning("обложка для черновика #%s не собрана: %s", draft.id, e)
        return None
    base = settings.media_base_url.strip().rstrip("/")
    return Cover(image=image, url=f"{base}/{name}" if base else None)
