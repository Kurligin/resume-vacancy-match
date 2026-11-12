"""Справочник профстандартов: типовые трудовые функции для скупо написанных вакансий."""

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "profstandards.json"


@dataclass(frozen=True)
class Standard:
    code: str
    name: str
    keywords: tuple[str, ...]  # начала слов должности, без регистра
    functions: tuple[str, ...]
    source: str


def _norm(text: str) -> str:
    return re.sub(r"[\s\-–]+", " ", text.lower().replace("ё", "е")).strip()


@cache
def standards() -> tuple[Standard, ...]:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    return tuple(
        Standard(s["code"], s["name"], tuple(s["keywords"]), tuple(s["functions"]), s["source"])
        for s in raw
    )


def match_title(title: str) -> Standard | None:
    """Профстандарт по названию должности: побеждает самое длинное ключевое слово."""
    norm = _norm(title)
    best: tuple[int, Standard] | None = None
    for st in standards():
        for kw in st.keywords:
            hit = re.search(r"(?<!\w)" + re.escape(_norm(kw)), norm)
            if hit and (best is None or len(kw) > best[0]):
                best = (len(kw), st)
    return best[1] if best else None


def functions(code: str) -> list[str]:
    """Типовые трудовые функции профстандарта; пусто, если кода нет в справочнике."""
    return next((list(s.functions) for s in standards() if s.code == code), [])
