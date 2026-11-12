"""Справочник навыков: синонимы и смежные навыки, поиск по строкам документа. Без сети.

Синоним в данных: слова через пробел/точку/дефис; `*` в конце слова — любое окончание
(«учет* основн* средств*»). Короткий синоним с заглавной буквой («ОС», «ТМЦ», «Go»)
ищется с учётом регистра, остальные — без.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from rvm.models import Line

DATA = Path(__file__).parent / "data" / "skills.json"

_SEP_CHARS = r"\s.\-_/:–"
_SEP = f"[{_SEP_CHARS}]+"


@dataclass(frozen=True)
class Entry:
    name: str
    area: str
    synonyms: tuple[str, ...]
    related: tuple[str, ...]


@dataclass(frozen=True)
class SkillHit:
    line: int
    canonical: str
    matched: str  # буквальный фрагмент строки — по нему код сверяет цитату


def key(term: str) -> str:
    """Ключ сравнения: регистр, ё, пробелы, точки, дефисы, слэши не важны."""
    return re.sub(f"[{_SEP_CHARS}]", "", term.lower().replace("ё", "е"))


def _token_regex(token: str) -> str:
    out = []
    for ch in token:
        if ch == "*":
            out.append(r"\w*")
        elif ch in "её":
            out.append("[её]")
        elif ch in "ЕЁ":
            out.append("[ЕЁ]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _synonym_regex(syn: str) -> str:
    tokens = [t for t in re.split(_SEP, syn.strip()) if t]
    body = f"[{_SEP_CHARS}]*".join(_token_regex(t) for t in tokens)
    compact = syn.strip()
    if len(compact) <= 3 and any(ch.isupper() for ch in compact):
        return rf"(?<!\w)(?:{body})(?!\w)"
    return rf"(?<!\w)(?i:{body})(?!\w)"


@cache
def _load() -> tuple[dict[str, Entry], dict[str, str], dict[str, re.Pattern[str]]]:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    entries: dict[str, Entry] = {}
    for item in raw:
        syns = (item["name"], *item["synonyms"], *item.get("hh_synonyms", []))
        entries[item["name"]] = Entry(item["name"], item["area"], syns, tuple(item["related"]))
    index: dict[str, str] = {}
    patterns: dict[str, re.Pattern[str]] = {}
    for e in entries.values():
        for s in e.synonyms:
            if "*" in s:
                continue
            owner = index.setdefault(key(s), e.name)
            if owner != e.name:
                raise ValueError(f"синоним «{s}» у двух навыков: {owner}, {e.name}")
        missing = [r for r in e.related if r not in entries]
        if missing:
            raise ValueError(f"{e.name}: смежные не из справочника: {missing}")
        alts = sorted(e.synonyms, key=len, reverse=True)
        patterns[e.name] = re.compile("|".join(_synonym_regex(s) for s in alts))
    return entries, index, patterns


def entries() -> list[Entry]:
    return list(_load()[0].values())


def canonical(term: str) -> str | None:
    """Каноническое имя навыка или None, если термина нет в справочнике."""
    _, index, patterns = _load()
    found = index.get(key(term))
    if found:
        return found
    text = term.strip()
    return next((name for name, p in patterns.items() if p.fullmatch(text)), None)


def related(a: str, b: str) -> bool:
    """Смежные навыки (дают «частично»). Один и тот же навык смежным не считается."""
    ca, cb = canonical(a), canonical(b)
    if ca is None or cb is None or ca == cb:
        return False
    entries_ = _load()[0]
    return cb in entries_[ca].related or ca in entries_[cb].related


def find_in_lines(lines: list[Line]) -> list[SkillHit]:
    """Навыки в строках: длинное совпадение важнее вложенного, пересечений нет."""
    patterns = _load()[2]
    hits: list[SkillHit] = []
    for ln in lines:
        spans = [
            (m.start(), m.end(), name)
            for name, p in patterns.items()
            for m in p.finditer(ln.text)
            if m.end() > m.start()
        ]
        spans.sort(key=lambda s: (s[0] - s[1], s[0]))
        taken: list[tuple[int, int, str]] = []
        for s in spans:
            if all(s[1] <= t[0] or s[0] >= t[1] for t in taken):
                taken.append(s)
        hits += [SkillHit(ln.n, name, ln.text[a:b]) for a, b, name in sorted(taken)]
    return hits


def hints(text: str) -> list[str]:
    """Канонические имена навыков из текста — подсказки для промпта, без повторов."""
    lines = [Line(n=i, text=t) for i, t in enumerate(text.splitlines(), 1)]
    return list(dict.fromkeys(h.canonical for h in find_in_lines(lines)))
