from __future__ import annotations

import json


def extract_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели, даже если она обернула его в ``` или добавила пояснение."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе модели нет JSON-объекта")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"ответ модели — не валидный JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("ответ модели — не JSON-объект")  # noqa: TRY004 — вызывающие ловят ValueError
    return data
