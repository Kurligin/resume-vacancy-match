"""Требования вакансии: модель извлекает пункты, раздел по заголовкам решает код."""

import re
from pathlib import Path

from pydantic import BaseModel

from rvm.llm import LLMClient
from rvm.models import Document, ReqKind, Requirement, Section

PROMPT_VERSION = "extract_vacancy.v1"
PROMPT = Path(__file__).resolve().parents[3] / "prompts" / f"{PROMPT_VERSION}.md"

# Начала заголовков, регистронезависимо. None — известный заголовок вне требований (сброс раздела).
HEADINGS: list[tuple[str, Section | None]] = [
    ("будет плюсом", Section.NICE),
    ("будет преимуществом", Section.NICE),
    ("преимуществом будет", Section.NICE),
    ("плюсом будет", Section.NICE),
    ("желательно", Section.NICE),
    ("приветствуется", Section.NICE),
    ("требования", Section.MUST),
    ("мы ожидаем", Section.MUST),
    ("ожидаем от", Section.MUST),
    ("что мы ждем", Section.MUST),
    ("что мы ждём", Section.MUST),
    ("обязательно", Section.MUST),
    ("обязанности", Section.INFERRED),
    ("задачи", Section.INFERRED),
    ("чем предстоит заниматься", Section.INFERRED),
    ("что нужно делать", Section.INFERRED),
    ("условия", Section.CONDITION),
    ("мы предлагаем", Section.CONDITION),
    ("что мы предлагаем", Section.CONDITION),
    ("предлагаем", Section.CONDITION),
    ("о компании", None),
    ("о нас", None),
    ("о проекте", None),
    ("описание", None),
]
BULLET = re.compile(r"^\s*([-–—•*·]|\d+[.)])")


class _Item(BaseModel):
    text: str
    section: Section
    kind: ReqKind
    years: float | None
    source_line: int | None


class _Items(BaseModel):
    items: list[_Item]


def _heading(text: str) -> tuple[bool, Section | None]:
    """Строка — известный заголовок? Пункты списка и длинные фразы заголовками не считаются."""
    if BULLET.match(text):
        return False, None
    norm = text.strip().lower()
    # shortcut: короткая строка «Желательно знание Vue» без маркера тоже станет заголовком
    if len(norm) > 60 or (not norm.endswith(":") and len(norm.split()) > 4):
        return False, None
    for prefix, section in HEADINGS:
        if norm.startswith(prefix):
            return True, section
    return False, None


def line_sections(doc: Document) -> dict[int, Section | None]:
    """Раздел каждой строки по последнему известному заголовку выше (или в ней самой)."""
    current: Section | None = None
    result: dict[int, Section | None] = {}
    for line in doc.lines:
        is_heading, section = _heading(line.text)
        if is_heading:
            current = section
        result[line.n] = current
    return result


def extract_requirements(doc: Document, llm: LLMClient) -> list[Requirement]:
    messages = [
        {"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": doc.numbered()},
    ]
    parsed: _Items = llm.complete(messages, _Items, PROMPT_VERSION)
    sections = line_sections(doc)
    reqs = []
    for i, item in enumerate(parsed.items, 1):
        line = item.source_line if item.source_line in sections else None
        section = (sections[line] if line is not None else None) or item.section
        reqs.append(
            Requirement(
                id=f"R{i}",
                text=item.text,
                section=section,
                kind=item.kind,
                years=item.years,
                source_line=line,
            )
        )
    return reqs
