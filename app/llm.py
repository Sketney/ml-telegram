"""Тонкая обёртка над LLM-провайдерами: один метод complete(system, user)."""

from __future__ import annotations

import json
from typing import Protocol

from app.config import Settings


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
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
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
        return json.dumps(
            {
                "html": (
                    f"<b>[заглушка] {topic}</b>\n\n"
                    "Это текст от FakeLLM: он проверяет, что бот, кнопки и расписание работают. "
                    "Поставь LLM_PROVIDER=anthropic или openai в .env, чтобы получать настоящие черновики.\n\n"
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
