"""Тонкая обёртка над LLM-провайдерами: один метод complete(system, user)."""

from __future__ import annotations

import json
import logging
from html import escape
from typing import Protocol

from app.config import Settings

log = logging.getLogger(__name__)

# Потолок ответа, а не расход. Запас нужен: актуальные модели Claude сначала рассуждают,
# и эти токены входят в тот же лимит, что и сам текст поста.
ANTHROPIC_MAX_TOKENS = 16000


class LLM(Protocol):
    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str: ...


class AnthropicLLM:
    def __init__(self, settings: Settings) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=settings.llm_api_key)
        self._settings = settings

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        resp = await self._client.messages.create(
            model=self._settings.model_for(cheap),
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "max_tokens":
            log.warning("ответ модели обрезан по max_tokens=%s", ANTHROPIC_MAX_TOKENS)
        return "".join(block.text for block in resp.content if block.type == "text")


class OpenAILLM:
    def __init__(self, settings: Settings) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=settings.llm_api_key)
        self._settings = settings

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        resp = await self._client.chat.completions.create(
            model=self._settings.model_for(cheap),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


class FakeLLM:
    """Заготовки без обращения к API: чтобы проверить бота и расписание до покупки ключа.

    Тексты нарочно помечены как заглушки — публиковать их в канал не нужно.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, *, cheap: bool = False) -> str:
        self.calls.append((system, user))
        topic = _extract_topic(user)
        if '"items": [{"id"' in system:  # запрос на оценку кандидатов из источников
            rows = json.loads(user[user.index("Материалы:\n") + len("Материалы:\n") :])
            return json.dumps(
                {"items": [{"id": r["id"], "score": 7, "why": "заглушка FakeLLM"} for r in rows]}, ensure_ascii=False
            )
        if '"unsupported"' in system:  # запрос на фактчек
            return json.dumps({"unsupported": []})
        if '"sections"' in system:  # шпаргалка
            return json.dumps(
                {
                    "title": f"[заглушка] {topic}"[:60],
                    "subtitle": "Заглушка FakeLLM: проверка механики шпаргалок",
                    "sections": [
                        {
                            "heading": "Блок 1",
                            "items": [{"term": "A", "text": "первая строка"}, {"term": "B", "text": "вторая"}],
                        },
                        {
                            "heading": "Блок 2",
                            "items": [{"term": "C", "text": "третья строка"}, {"term": "D", "text": "четвёртая"}],
                        },
                    ],
                    "takeaway": "Подключи настоящую модель в .env.",
                    "caption": f"<b>Шпаргалка: {escape(topic)}</b>\n\nЗаглушка FakeLLM. "
                    + "Текст подписи для проверки длины. " * 5,
                },
                ensure_ascii=False,
            )
        if '"question"' in system:
            return json.dumps(
                {
                    "question": f"[заглушка] Вопрос по теме «{topic}»?"[:300],
                    "options": ["Верный вариант", "Заблуждение 1", "Заблуждение 2", "Заблуждение 3"],
                    "correct_index": 0,
                    "explanation": "Заглушка FakeLLM: подключи настоящую модель в .env.",
                },
                ensure_ascii=False,
            )
        own_block = ""  # рубрики с блоком, который автор вписывает сам
        for name in ("Мой опыт", "Моё мнение"):
            if "{" + name + "}" in system:
                own_block = f"<b>{name}:</b> {{{name}}}\n\n"
        return json.dumps(
            {
                "html": (
                    f"<b>[заглушка] {escape(topic)}</b>\n\n"
                    "Это текст от FakeLLM: он проверяет, что бот, кнопки и расписание работают. "
                    "Поставь LLM_PROVIDER=anthropic или openai в .env, чтобы получать настоящие черновики.\n\n"
                    f"{own_block}"
                    "<blockquote expandable>Здесь будет блок с нюансами.</blockquote>"
                )
            },
            ensure_ascii=False,
        )


def _extract_topic(user: str) -> str:
    for line in user.splitlines():
        if line.startswith("Тема:"):
            return line.removeprefix("Тема:").strip()
    return "без темы"


def make_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings)
    if settings.llm_provider == "openai":
        return OpenAILLM(settings)
    if settings.llm_provider == "fake":
        return FakeLLM()
    raise RuntimeError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")
