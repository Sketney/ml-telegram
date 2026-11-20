"""Второй проход: сверка поста с материалом, по которому он написан."""

from __future__ import annotations

import logging

from app.config import PROMPTS_DIR
from app.jsonutil import extract_json_object
from app.llm import LLM

log = logging.getLogger(__name__)

MAX_FINDINGS = 6


async def check(llm: LLM, post_html: str, material: str) -> list[dict]:
    """Возвращает предупреждения для карточки черновика. Сбой проверки — тоже предупреждение, а не ошибка."""
    system = (PROMPTS_DIR / "factcheck.md").read_text(encoding="utf-8").strip()
    user = f"<material>\n{material}\n</material>\n\n<post>\n{post_html}\n</post>"
    try:
        data = extract_json_object(await llm.complete(system, user, cheap=True))
    except Exception as e:  # noqa: BLE001 — сеть, лимиты, неразборчивый ответ: пост от этого хуже не стал
        log.warning("фактчек не выполнен: %s", e)
        return [
            {"level": "warn", "text": "фактчек не выполнен, сверь цифры с источником вручную", "factcheck": "failed"}
        ]
    findings = []
    for row in (data.get("unsupported") or [])[:MAX_FINDINGS]:
        if not isinstance(row, dict) or not str(row.get("claim") or "").strip():
            continue
        claim, why = str(row["claim"]).strip()[:160], str(row.get("why") or "").strip()[:160]
        findings.append(
            {"level": "warn", "text": f"фактчек: «{claim}»" + (f" — {why}" if why else ""), "factcheck": "claim"}
        )
    return findings
