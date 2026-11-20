"""HTTP-запросы к источникам: с таймаутом, ограничением размера и понятной ошибкой."""

from __future__ import annotations

import json
import re
from html import unescape

import aiohttp

USER_AGENT = "ml-telegram-bot/0.2 (+https://github.com/Sketney/ml-telegram)"
MAX_BYTES = 6_000_000

_DROP_BLOCKS_RE = re.compile(r"<(script|style|noscript|svg|nav|header|footer)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n\s*\n+")


class FetchError(Exception):
    pass


async def fetch_text(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30) -> str:
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"User-Agent": USER_AGENT, **(headers or {})},
                trust_env=True,  # уважает HTTPS_PROXY, если бот запущен за прокси
            ) as session,
            session.get(url) as resp,
        ):
            if resp.status >= 400:
                raise FetchError(f"{url}: HTTP {resp.status}")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FetchError(f"{url}: ответ больше {MAX_BYTES // 1_000_000} МБ")
            return b"".join(chunks).decode(resp.charset or "utf-8", errors="replace")
    except FetchError:
        raise
    except Exception as e:  # сеть, DNS, таймаут — вызывающему важна только причина
        raise FetchError(f"{url}: {type(e).__name__}: {e}") from e


async def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30):
    text = await fetch_text(url, headers=headers, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"{url}: ответ не JSON ({e})") from e


_MD_HTML_RE = re.compile(
    r"</?(div|img|p|a|br|hr|table|thead|tbody|tr|td|th|span|picture|source|center|sub|sup|details|summary"
    r"|b|i|strong|em|kbd|h[1-6]|ul|ol|li|video|iframe)\b[^>]*>",
    re.IGNORECASE,
)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def clean_markdown(md: str) -> str:
    """README без HTML-вёрстки, бейджей и картинок: чтобы лимит символов уходил на содержание."""
    text = _MD_COMMENT_RE.sub("", md)
    text = _MD_IMAGE_RE.sub("", text)
    text = unescape(_MD_HTML_RE.sub(" ", text))
    text = _SPACE_RE.sub(" ", text)
    return _BLANK_RE.sub("\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()


def html_to_text(html: str) -> str:
    """Грубое извлечение текста из HTML: без скриптов, стилей и навигации."""
    text = _DROP_BLOCKS_RE.sub(" ", html)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = unescape(_TAG_RE.sub(" ", text))
    text = _SPACE_RE.sub(" ", text)
    return _BLANK_RE.sub("\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()
