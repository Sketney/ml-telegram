"""Развёрнутый пост (rich message): блоки от модели → проверка → блоки Telegram.

Модель возвращает короткий список блоков простого вида (абзац, заголовок, список, таблица, код, цитата,
вопрос с ответом). Здесь он приводится к одному виду, проверяется и превращается в сообщение Telegram.
Рядом хранится запасной текст в обычной разметке: по нему считается длина, пишется квиз к уроку,
и он же показывается автору, если Telegram развёрнутое сообщение не принял.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser

from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockExpandableBlockQuotation,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockPreformatted,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichTextBold,
    RichTextCode,
    RichTextHashtag,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUnderline,
    RichTextUrl,
)

from app.validator import issue, validate_post_html, visible_len

HEADING_LIMIT = 70
TABLE_MAX_COLS = 3
TABLE_MAX_ROWS = 6
TABLE_FIRST_CELL = 26  # первый столбец — термин: на телефоне он узкий
TABLE_CELL = 48
LIST_MAX_ITEMS = 8
NO_LIMIT = 10**6

_WRAP = {
    "b": RichTextBold,
    "strong": RichTextBold,
    "i": RichTextItalic,
    "em": RichTextItalic,
    "u": RichTextUnderline,
    "s": RichTextStrikethrough,
    "code": RichTextCode,
}


class _Inline(HTMLParser):
    """Строка с простыми тегами (<b>, <i>, <code>, <a href>) → вложенный текст Telegram."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.frames: list[tuple[str, str, list]] = [("", "", [])]

    def handle_starttag(self, tag, attrs):
        if tag in _WRAP or tag == "a":
            self.frames.append((tag, dict(attrs).get("href") or "", []))

    def handle_endtag(self, tag):
        if len(self.frames) > 1 and self.frames[-1][0] == tag:
            self._close()

    def handle_data(self, data):
        if data:
            self.frames[-1][2].append(data)

    def _close(self) -> None:
        tag, href, parts = self.frames.pop()
        text = _join(parts)
        if not text:
            return
        if tag == "a":
            node = RichTextUrl(text=text, url=href) if href.startswith(("http://", "https://", "tg://")) else text
        else:
            node = _WRAP[tag](text=text)
        self.frames[-1][2].append(node)

    def result(self):
        while len(self.frames) > 1:
            self._close()
        return _join(self.frames[0][2])


def _join(parts: list):
    if all(isinstance(p, str) for p in parts):
        return "".join(parts)
    return parts[0] if len(parts) == 1 else parts


def inline(text: str):
    parser = _Inline()
    parser.feed(text or "")
    parser.close()
    return parser.result()


def _plain(text: str) -> str:
    """Текст без тегов: для заголовков и ячеек таблиц."""
    parser = _Inline()
    parser.feed(text or "")
    parser.close()
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        else:
            walk(node.text)

    walk(parser.result())
    return " ".join("".join(out).split())


def normalize(raw, brand_emoji: str = "") -> list[dict]:
    """Блоки из ответа модели → блоки известного вида. Неизвестное и пустое отбрасывается."""

    def text_of(value) -> str:
        return str(value or "").strip().replace("{E}", brand_emoji)

    blocks: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and text_of(item.get("text")):
            blocks.append({"type": "text", "text": text_of(item.get("text"))})
        elif kind == "heading" and text_of(item.get("text")):
            blocks.append({"type": "heading", "text": _plain(text_of(item.get("text")))})
        elif kind == "list" and isinstance(item.get("items"), list):
            items = [text_of(x) for x in item["items"] if text_of(x)]
            if items:
                blocks.append({"type": "list", "ordered": bool(item.get("ordered")), "items": items})
        elif kind == "table" and isinstance(item.get("rows"), list):
            rows = [[_plain(text_of(c)) for c in row] for row in item["rows"] if isinstance(row, list) and row]
            header = item.get("header")
            header = [_plain(text_of(c)) for c in header] if isinstance(header, list) and header else None
            if rows:
                blocks.append({"type": "table", "header": header, "rows": rows})
        elif kind == "code" and str(item.get("text") or "").strip():
            blocks.append(
                {
                    "type": "code",
                    "language": str(item.get("language") or "").strip(),
                    "text": str(item["text"]).strip("\n"),
                }
            )
        elif kind == "quote" and text_of(item.get("text")):
            blocks.append({"type": "quote", "text": text_of(item.get("text"))})
        elif kind == "check" and text_of(item.get("question")) and text_of(item.get("answer")):
            blocks.append({"type": "check", "question": text_of(item["question"]), "answer": text_of(item["answer"])})
        elif kind == "divider":
            blocks.append({"type": "divider"})
    return blocks


def to_html(payload: dict) -> str:
    """Тот же пост обычной разметкой Telegram: запасной вид и основа для подсчёта длины."""
    parts = [f"<b>{escape(payload.get('title') or '')}</b>"] if payload.get("title") else []
    for b in payload.get("blocks") or []:
        kind = b["type"]
        if kind == "text":
            parts.append(b["text"])
        elif kind == "heading":
            parts.append(f"<b>{escape(b['text'])}</b>")
        elif kind == "list":
            parts.append("\n".join(f"{n}. {x}" if b["ordered"] else f"• {x}" for n, x in enumerate(b["items"], 1)))
        elif kind == "table":
            rows = ([b["header"]] if b.get("header") else []) + b["rows"]
            parts.append("\n".join(escape(" — ".join(c for c in row if c)) for row in rows))
        elif kind == "code":
            parts.append(f"<pre>{escape(b['text'])}</pre>")
        elif kind == "quote":
            parts.append(f"<blockquote expandable>{b['text']}</blockquote>")
        elif kind == "check":
            parts.append(f"{b['question']}\n<tg-spoiler>{b['answer']}</tg-spoiler>")
    if payload.get("tail"):
        parts.append(payload["tail"])
    return "\n\n".join(parts)


def validate(payload: dict, *, min_len: int = 0, max_len: int = NO_LIMIT) -> list[dict]:
    blocks = payload.get("blocks") or []
    if len(blocks) < 4:
        return [issue("error", f"в посте {len(blocks)} блоков: это не развёрнутый пост")]
    # разметка, заглушки, стоп-фразы и длина проверяются по запасному тексту
    issues = [
        i
        for i in validate_post_html(payload.get("html") or "", min_len=min_len, max_len=max_len, hard_limit=NO_LIMIT)
        if "жирных выделений" not in i["text"]  # заголовки разделов в запасном тексте жирные — это не выделения
    ]
    for b in blocks:
        if b["type"] == "heading" and len(b["text"]) > HEADING_LIMIT:
            issues.append(issue("warn", f"заголовок раздела длиннее {HEADING_LIMIT} символов: «{b['text'][:40]}…»"))
        elif b["type"] == "list" and len(b["items"]) > LIST_MAX_ITEMS:
            issues.append(issue("warn", f"в списке {len(b['items'])} пунктов, лучше не больше {LIST_MAX_ITEMS}"))
        elif b["type"] == "table":
            rows = ([b["header"]] if b.get("header") else []) + b["rows"]
            cols = max(len(r) for r in rows)
            name = f"таблица «{(b['rows'][0][0] or '…')[:20]}…»"
            if cols > TABLE_MAX_COLS or cols < 2:
                issues.append(
                    issue(
                        "error", f"{name}: столбцов {cols}, нужно 2 или {TABLE_MAX_COLS} — шире на телефоне не читается"
                    )
                )
            if len({len(r) for r in rows}) > 1:
                issues.append(issue("error", f"{name}: в строках разное число ячеек"))
            if len(b["rows"]) > TABLE_MAX_ROWS:
                issues.append(issue("error", f"{name}: строк {len(b['rows'])}, нужно не больше {TABLE_MAX_ROWS}"))
            long = [
                c for r in b["rows"] for n, c in enumerate(r) if len(c) > (TABLE_FIRST_CELL if n == 0 else TABLE_CELL)
            ]
            if long:
                issues.append(
                    issue(
                        "error",
                        f"{name}: ячейка слишком длинная («{long[0][:30]}…»). Первый столбец до "
                        f"{TABLE_FIRST_CELL} символов, остальные до {TABLE_CELL}: длинное — списком, а не таблицей",
                    )
                )
    return issues


def to_telegram(payload: dict, *, cover: bytes | None = None, hashtag: str = "") -> InputRichMessage:
    """Сообщение Telegram. Обложка ставится внутрь поста; без неё пост начинается с заголовка."""
    out: list = []
    if cover:
        out.append(InputRichBlockPhoto(photo=InputMediaPhoto(media=BufferedInputFile(cover, filename="cover.jpg"))))
    elif payload.get("title"):
        out.append(InputRichBlockSectionHeading(text=payload["title"], size=2))
    for b in payload.get("blocks") or []:
        kind = b["type"]
        if kind == "text":
            out.append(InputRichBlockParagraph(text=inline(b["text"])))
        elif kind == "heading":
            out.append(InputRichBlockSectionHeading(text=b["text"], size=3))
        elif kind == "list":
            out.append(
                InputRichBlockList(
                    items=[
                        InputRichBlockListItem(
                            blocks=[InputRichBlockParagraph(text=inline(x))],
                            value=n if b["ordered"] else None,
                            type="1" if b["ordered"] else None,
                        )
                        for n, x in enumerate(b["items"], 1)
                    ]
                )
            )
        elif kind == "table":
            cells = []
            if b.get("header"):
                cells.append(
                    [RichBlockTableCell(align="left", valign="top", text=c, is_header=True) for c in b["header"]]
                )
            cells += [[RichBlockTableCell(align="left", valign="top", text=c) for c in row] for row in b["rows"]]
            out.append(InputRichBlockTable(cells=cells, is_bordered=True, is_striped=True, is_compact=True))
        elif kind == "code":
            out.append(InputRichBlockPreformatted(text=b["text"], language=b["language"] or None))
        elif kind == "quote":
            out.append(InputRichBlockExpandableBlockQuotation(text=inline(b["text"])))
        elif kind == "check":
            out.append(InputRichBlockParagraph(text=[RichTextBold(text="Проверь себя. "), inline(b["question"])]))
            out.append(
                InputRichBlockDetails(summary="Ответ", blocks=[InputRichBlockParagraph(text=inline(b["answer"]))])
            )
        elif kind == "divider":
            out.append(InputRichBlockDivider())
    footer: list = []
    if payload.get("nav"):
        footer += [inline(payload["nav"]), " · "]
    if hashtag:
        footer.append(RichTextHashtag(text=hashtag, hashtag=hashtag.lstrip("#")))
    if payload.get("signature"):
        footer += [" · ", payload["signature"]]
    if footer:
        out.append(InputRichBlockFooter(text=footer))
    return InputRichMessage(blocks=out)


def length(payload: dict) -> int:
    return visible_len(payload.get("html") or "")
