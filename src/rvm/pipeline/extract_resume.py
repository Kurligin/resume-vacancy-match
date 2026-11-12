"""Факты резюме: модель размечает строки и сырые даты, даты и длительности разбирает код."""

from pathlib import Path

from pydantic import BaseModel

from rvm.experience import parse_dates, parse_duration
from rvm.facts import Education, Period, ResumeFacts
from rvm.llm import LLMClient
from rvm.models import Document

PROMPT_VERSION = "extract_resume.v1"
PROMPT = Path(__file__).resolve().parents[3] / "prompts" / f"{PROMPT_VERSION}.md"


class _Period(BaseModel):
    org: str | None
    title: str | None
    line_from: int
    line_to: int
    dates_raw: str
    declared_raw: str | None


class _Markup(BaseModel):
    periods: list[_Period]
    education: list[Education]
    city: str | None
    work_formats: list[str]
    declared_total_raw: str | None
    letter_lines: list[int]


def _squash(text: str) -> str:
    return " ".join(text.split()).lower()


def extract_resume_facts(doc: Document, llm: LLMClient) -> ResumeFacts:
    messages = [
        {"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": doc.numbered()},
    ]
    markup: _Markup = llm.complete(messages, _Markup, PROMPT_VERSION)
    warnings = []
    periods = []
    for item in markup.periods:
        p = Period(**item.model_dump())
        block = _squash(" ".join(ln.text for ln in doc.lines if p.line_from <= ln.n <= p.line_to))
        where = f"в строках {p.line_from}–{p.line_to}"
        if p.declared_raw and _squash(p.declared_raw) in block:
            p.declared_months = parse_duration(p.declared_raw)
        elif p.declared_raw:
            warnings.append(f"длительность «{p.declared_raw}» не найдена {where}")
        if _squash(p.dates_raw) not in block:
            warnings.append(f"даты «{p.dates_raw}» не найдены {where}")
            periods.append(p)  # даты модели без опоры на текст не разбираем
            continue
        parsed = parse_dates(p.dates_raw)
        if parsed:
            p.start, p.end, p.current = parsed
        else:
            warnings.append(f"не разобраны даты «{p.dates_raw}» (строка {p.line_from})")
        periods.append(p)
    numbers = {ln.n for ln in doc.lines}
    total_raw = markup.declared_total_raw
    if total_raw and _squash(total_raw) not in _squash(" ".join(ln.text for ln in doc.lines)):
        warnings.append(f"общий стаж «{total_raw}» не найден в тексте")
        total_raw = None
    return ResumeFacts(
        periods=periods,
        education=markup.education,
        city=markup.city,
        work_formats=markup.work_formats,
        declared_total_raw=markup.declared_total_raw,
        declared_total_months=parse_duration(total_raw),
        letter_lines=[n for n in markup.letter_lines if n in numbers],
        warnings=warnings,
    )
