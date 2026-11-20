from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import DATA_DIR, PROMPTS_DIR
from app.validator import TG_TEXT_LIMIT

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Rubric:
    key: str
    title: str
    hashtag: str
    kind: str  # "post" | "quiz" | "sheet" (картинка-шпаргалка с подписью)
    min_len: int = 0
    max_len: int = TG_TEXT_LIMIT
    # "backlog" — темы из бэклога; "feed" — по материалам из источников; "notes" — по заметкам автора
    source: str = "backlog"
    # Рубрика по бэклогу, в которой пост пишется только по фактам и ссылке, приложенным к теме.
    grounded: bool = False
    # Делать ли посту обложку (только для kind: post). Выключается строкой cover: false в rubrics.yaml.
    cover: bool = True


@lru_cache
def load_rubrics(path: str | None = None) -> dict[str, Rubric]:
    raw = yaml.safe_load(Path(path or DATA_DIR / "rubrics.yaml").read_text(encoding="utf-8")) or {}
    rubrics = {}
    for key, cfg in raw.items():
        kind = cfg.get("kind", "post")
        if kind not in ("post", "quiz", "sheet"):
            raise ValueError(f"rubrics.yaml: у {key} неизвестный kind={kind}")
        source = cfg.get("source", "backlog")
        if source not in ("backlog", "feed", "notes"):
            raise ValueError(f"rubrics.yaml: у {key} неизвестный source={source}")
        rubrics[key] = Rubric(
            key=key,
            title=cfg["title"],
            hashtag=cfg.get("hashtag", ""),
            kind=kind,
            min_len=int(cfg.get("min_len", 0)),
            max_len=int(cfg.get("max_len", TG_TEXT_LIMIT)),
            source=source,
            grounded=bool(cfg.get("grounded", False)),
            cover=kind == "post" and bool(cfg.get("cover", True)),
        )
    return rubrics


def get_rubric(key: str) -> Rubric:
    rubrics = load_rubrics()
    if key not in rubrics:
        raise KeyError(f"Нет рубрики «{key}». Есть: {', '.join(rubrics)}")
    return rubrics[key]


def _read(path: Path) -> str:
    return _COMMENT_RE.sub("", path.read_text(encoding="utf-8")).strip()


def load_examples(rubric_key: str, prompts_dir: Path = PROMPTS_DIR) -> list[str]:
    folder = prompts_dir / "examples" / rubric_key
    if not folder.is_dir():
        return []
    files = sorted(p for p in folder.iterdir() if p.suffix in (".md", ".html", ".json", ".txt"))
    return [text for p in files if (text := _read(p))]


def build_system_prompt(rubric_key: str, brand_emoji: str, prompts_dir: Path = PROMPTS_DIR) -> str:
    parts = [
        _read(prompts_dir / "styleguide.md"),
        _read(prompts_dir / "rubrics" / f"{rubric_key}.md"),
    ]
    examples = load_examples(rubric_key, prompts_dir)
    if examples:
        block = "\n\n---\n\n".join(examples)
        parts.insert(
            1,
            "# Эталонные посты автора\n\n"
            "Ниже посты, написанные автором вручную. Перенимай их тон, ритм и лексику, "
            "но не копируй содержание и формулировки.\n\n" + block,
        )
    return "\n\n".join(parts).replace("{E}", brand_emoji)
