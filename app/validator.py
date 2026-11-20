"""Проверки черновика до отправки на модерацию.

error — публиковать нельзя (Telegram отклонит сообщение или в тексте осталась заглушка).
warn  — можно, но автору стоит посмотреть.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

TG_TEXT_LIMIT = 4096
QUIZ_QUESTION_LIMIT = 300
QUIZ_OPTION_LIMIT = 100
QUIZ_EXPLANATION_LIMIT = 200
QUIZ_MAX_OPTIONS = 10
# Рекомендованная длина — ориентир: о выходе за неё на несколько символов не предупреждаем.
LENGTH_TOLERANCE = 0.1

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
}  # fmt: skip
ALLOWED_ATTRS = {
    "a": {"href"},
    "span": {"class"},
    "code": {"class"},
    "pre": {"language"},
    "blockquote": {"expandable"},
    "tg-emoji": {"emoji-id"},
}
# Внутри code/pre Telegram не разбирает вложенную разметку (кроме <pre><code>).
RAW_TAGS = {"code", "pre"}

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*)?>")
_PLACEHOLDER_RE = re.compile(r"\{[^{}\n]{1,80}\}")

STOP_PHRASES = [
    "в современном мире",
    "не секрет, что",
    "давайте разберёмся",
    "давайте разберемся",
    "в этой статье",
    "революционн",
    "переворачивает игру",
    "без преувеличения",
    "играет ключевую роль",
    "является неотъемлемой частью",
    "таким образом, можно сделать вывод",
]


def issue(level: str, text: str) -> dict:
    return {"level": level, "text": text}


def utf16_len(text: str) -> int:
    """Telegram считает длину в кодовых единицах UTF-16 (эмодзи = 2)."""
    return len(text.encode("utf-16-le")) // 2


class _TgHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.visible: list[str] = []  # весь видимый текст
        self.prose: list[str] = []  # видимый текст вне code/pre

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"тег <{tag}> Telegram не поддерживает")
            return
        if self.stack and self.stack[-1] in RAW_TAGS and not (self.stack[-1] == "pre" and tag == "code"):
            self.errors.append(f"тег <{tag}> внутри <{self.stack[-1]}>: вложенная разметка там не работает")
        allowed = ALLOWED_ATTRS.get(tag, set())
        for name, value in attrs:
            if name not in allowed:
                self.errors.append(f"атрибут {name} у <{tag}> не поддерживается")
            elif tag == "span" and value != "tg-spoiler":
                self.errors.append('у <span> допустим только class="tg-spoiler"')
        if tag == "a" and not dict(attrs).get("href"):
            self.errors.append("<a> без href")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            return  # уже сообщили на открывающем
        if not self.stack or self.stack[-1] != tag:
            opened = f"<{self.stack[-1]}>" if self.stack else "ничего"
            self.errors.append(f"закрыт </{tag}>, а открыт {opened}")
            if tag in self.stack:  # восстановиться, чтобы не сыпать каскадом
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.visible.append(data)
        if not any(t in RAW_TAGS for t in self.stack):
            self.prose.append(data)


def validate_post_html(html: str, *, min_len: int = 0, max_len: int = TG_TEXT_LIMIT) -> list[dict]:
    issues: list[dict] = []
    if not html.strip():
        return [issue("error", "пустой текст")]

    # Каждый '<' должен начинать разрешённый тег, иначе Telegram вернёт "can't parse entities".
    for m in re.finditer(r"<", html):
        if not _TAG_RE.match(html, m.start()):
            around = html[max(0, m.start() - 15) : m.start() + 15].replace("\n", " ")
            issues.append(issue("error", f"неэкранированный символ «<» (нужно &lt;) рядом с «{around}»"))
            break

    parser = _TgHtmlParser()
    parser.feed(html)
    parser.close()
    issues += [issue("error", e) for e in dict.fromkeys(parser.errors)]
    for tag in parser.stack:
        issues.append(issue("error", f"тег <{tag}> не закрыт"))

    visible = "".join(parser.visible)
    prose = "".join(parser.prose)
    length = utf16_len(visible)
    if length > TG_TEXT_LIMIT:
        issues.append(issue("error", f"длина {length} символов, лимит Telegram — {TG_TEXT_LIMIT}"))
    elif length > max_len * (1 + LENGTH_TOLERANCE):
        issues.append(issue("warn", f"длина {length} символов, для рубрики рекомендовано до {max_len}"))
    elif length < min_len * (1 - LENGTH_TOLERANCE):
        issues.append(issue("warn", f"длина {length} символов, для рубрики рекомендовано от {min_len}"))

    for ph in dict.fromkeys(_PLACEHOLDER_RE.findall(prose)):
        issues.append(issue("error", f"осталась заглушка {ph} — её нужно заполнить"))

    low = prose.lower()
    for phrase in STOP_PHRASES:
        if phrase in low:
            issues.append(issue("warn", f"стоп-фраза из стайлгайда: «{phrase}»"))

    bold = len(re.findall(r"<(?:b|strong)>", html))
    if bold > 8:
        issues.append(issue("warn", f"жирных выделений {bold}, по стайлгайду не больше 5–7"))
    return issues


def validate_quiz(payload: dict) -> list[dict]:
    issues: list[dict] = []
    question = str(payload.get("question") or "").strip()
    options = payload.get("options")
    explanation = str(payload.get("explanation") or "").strip()
    correct = payload.get("correct_index")

    if not question:
        issues.append(issue("error", "нет вопроса"))
    elif utf16_len(question) > QUIZ_QUESTION_LIMIT:
        issues.append(issue("error", f"вопрос длиннее {QUIZ_QUESTION_LIMIT} символов"))

    if not isinstance(options, list) or not all(isinstance(o, str) and o.strip() for o in options):
        issues.append(issue("error", "варианты ответа должны быть непустыми строками"))
    else:
        if not 2 <= len(options) <= QUIZ_MAX_OPTIONS:
            issues.append(issue("error", f"вариантов {len(options)}, допустимо от 2 до {QUIZ_MAX_OPTIONS}"))
        for o in options:
            if utf16_len(o) > QUIZ_OPTION_LIMIT:
                issues.append(issue("error", f"вариант длиннее {QUIZ_OPTION_LIMIT} символов: «{o[:40]}…»"))
        if len({o.strip().lower() for o in options}) != len(options):
            issues.append(issue("error", "варианты ответа повторяются"))
        if not isinstance(correct, int) or isinstance(correct, bool) or not 0 <= correct < len(options):
            issues.append(issue("error", "correct_index не указывает на существующий вариант"))

    if not explanation:
        issues.append(issue("warn", "нет объяснения (explanation) — по формату рубрики оно обязательно"))
    elif utf16_len(explanation) > QUIZ_EXPLANATION_LIMIT:
        issues.append(issue("error", f"объяснение длиннее {QUIZ_EXPLANATION_LIMIT} символов"))

    for field in (question, explanation):
        for ph in _PLACEHOLDER_RE.findall(field):
            issues.append(issue("error", f"осталась заглушка {ph}"))
    return issues


def validate_payload(payload: dict, *, min_len: int = 0, max_len: int = TG_TEXT_LIMIT) -> list[dict]:
    if payload.get("kind") == "quiz":
        return validate_quiz(payload)
    return validate_post_html(str(payload.get("html") or ""), min_len=min_len, max_len=max_len)
