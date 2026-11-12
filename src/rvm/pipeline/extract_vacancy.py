"""Требования вакансии: модель извлекает пункты, раздел по заголовкам решает код."""

import re
from importlib.resources import files

from pydantic import BaseModel

from rvm.llm import LLMClient
from rvm.models import Document, ReqKind, Requirement, Section

PROMPT_VERSION = "extract_vacancy.v2"
PROMPT = files("rvm") / "prompts" / f"{PROMPT_VERSION}.md"

# Начала заголовков, регистронезависимо, «ё» как «е». Порядок важен: длинное раньше короткого.
# None — известный заголовок вне требований (сброс раздела).
HEADINGS: list[tuple[str, Section | None]] = [
    ("будет плюсом", Section.NICE),
    ("будет преимуществом", Section.NICE),
    ("преимуществом будет", Section.NICE),
    ("плюсом будет", Section.NICE),
    ("желательно", Section.NICE),
    ("приветствуется", Section.NICE),
    ("nice to have", Section.NICE),
    ("would be a plus", Section.NICE),
    ("will be a plus", Section.NICE),
    ("требования", Section.MUST),
    ("мы ожидаем", Section.MUST),
    ("ожидаем от", Section.MUST),
    ("наши ожидания", Section.MUST),
    ("что мы ждем", Section.MUST),
    ("мы ждем от", Section.MUST),
    ("обязательно", Section.MUST),
    ("кого мы ищем", Section.MUST),
    ("ты нам подходишь, если", Section.MUST),
    ("вы нам подходите, если", Section.MUST),
    ("необходимые навыки", Section.MUST),
    ("ключевые навыки", Section.MUST),
    ("что нужно делать", Section.INFERRED),
    ("что нужно", Section.MUST),
    ("requirements", Section.MUST),
    ("must have", Section.MUST),
    ("обязанности", Section.INFERRED),
    ("задачи", Section.INFERRED),
    ("чем предстоит заниматься", Section.INFERRED),
    ("responsibilities", Section.INFERRED),
    ("условия", Section.CONDITION),
    ("мы предлагаем", Section.CONDITION),
    ("что мы предлагаем", Section.CONDITION),
    ("предлагаем", Section.CONDITION),
    ("what we offer", Section.CONDITION),
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


def _norm(text: str) -> str:
    return text.strip().lower().replace("ё", "е")


def _key(norm: str) -> tuple[str, Section | None] | None:
    return next(((p, sec) for p, sec in HEADINGS if norm.startswith(p)), None)


def _heading(texts: list[str], i: int) -> tuple[bool, Section | None]:
    """Строка — заголовок, и какой у него раздел (None — не знаем, решает модель)."""
    text = texts[i]
    norm = _norm(text)
    if BULLET.match(text) or not norm or len(norm) > 60:
        return False, None
    colon = norm.endswith(":")
    next_bullet = i + 1 < len(texts) and bool(BULLET.match(texts[i + 1]))
    prev_bullet = i > 0 and bool(BULLET.match(texts[i - 1]))
    key = _key(norm)
    if key:
        tail = norm[len(key[0]) :].strip(" :.;!")
        # «Желательно знание Vue» — пункт: после ключевого слова есть содержание, вид не заголовка
        if colon or not tail or text.isupper() or (next_bullet and not prev_bullet):
            return True, key[1]
        return False, None
    if len(norm.split()) <= 6 and not norm.endswith(".") and (colon or next_bullet):
        return True, None
    return False, None


def line_sections(doc: Document) -> dict[int, Section | None]:
    """Раздел каждой строки по последнему заголовку выше (или в ней самой)."""
    texts = [line.text for line in doc.lines]
    current: Section | None = None
    result: dict[int, Section | None] = {}
    for i, line in enumerate(doc.lines):
        is_heading, section = _heading(texts, i)
        if is_heading:
            current = section
        # пункт со словом-ключом («Обязательно знание Go» под «Плюсом») — решает модель
        result[line.n] = current if is_heading or not _key(_norm(line.text)) else None
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
