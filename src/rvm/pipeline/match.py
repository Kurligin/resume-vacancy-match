"""Сопоставление требований с резюме: модель предлагает статус и цитаты, код проверяет и решает."""

import re
from collections.abc import Callable
from datetime import date
from importlib.resources import files

from pydantic import BaseModel

from rvm.experience import period_mentions, total_months
from rvm.facts import Period, ResumeFacts
from rvm.llm import LLMClient, LLMError
from rvm.models import Document, Evidence, MatchRow, ReqKind, Requirement, Status
from rvm.pipeline.verify import check, normalize
from rvm.refs.skills import hints, mentioned

PROMPT_VERSION = "match.v1"
PROMPT = files("rvm") / "prompts" / f"{PROMPT_VERSION}.md"
SELF_BLOCKS = {"Обо мне"}
# признаки отрицания: «нет» модели без них — не «нет»
NEGATION = re.compile(
    r"(?<!\w)(?:не|нет|без|ни|никогда|незнаком\w*|отсутств\w*|not|no|never|without"
    r"|готов\w* (?:освоить|изучить|научиться))(?!\w)|n't",
    re.IGNORECASE,
)
# общие слова требований: совпадение по ним не делает цитату относящейся к делу
_GENERIC = {"опыт", "знани", "умени", "навык", "владе", "работ", "испол", "понима", "experience"}

Skills = Callable[[str], list[str]]  # навык → варианты написания (справочник синонимов)


class _Quote(BaseModel):
    line: int
    quote: str


class _Exceeds(BaseModel):
    why: str
    line: int
    quote: str


class _Answer(BaseModel):
    requirement_id: str
    status: Status
    evidence: list[_Quote]
    exceeds: _Exceeds | None
    note: str
    self_claim: bool
    skill: str | None


class _Answers(BaseModel):
    items: list[_Answer]


def _ask(
    reqs: list[Requirement], doc: Document, llm: LLMClient, hint: str = ""
) -> dict[str, _Answer]:
    listing = "\n".join(
        f"{r.id}: {r.text} ({r.kind.value}"
        + (f", лет: {r.years:g}" if r.years is not None else "")
        + ")"
        for r in reqs
    )
    user = f"Резюме:\n{doc.numbered()}\n\nТребования:\n{listing}"
    messages = [
        {"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": user + (f"\n\n{hint}" if hint else "")},
    ]
    parsed: _Answers = llm.complete(messages, _Answers, PROMPT_VERSION)
    ids = {r.id for r in reqs}
    return {a.requirement_id: a for a in parsed.items if a.requirement_id in ids}


def _verified(a: _Answer, doc: Document, facts: ResumeFacts) -> list[Evidence] | None:
    """Все цитаты проверены — список доказательств, хоть одна не прошла — None."""
    out = [check(doc, facts, q.line, q.quote) for q in a.evidence]
    return None if None in out else [e for e in out if e is not None]


def _period_evidence(periods: list[Period], doc: Document, facts: ResumeFacts) -> list[Evidence]:
    """Строка с датами каждого места работы (или первая строка места)."""
    out = []
    for p in periods:
        lines = [ln for ln in doc.lines if p.line_from <= ln.n <= p.line_to]
        line = next((ln for ln in lines if p.dates_raw in ln.text), lines[0] if lines else None)
        ev = line and check(doc, facts, line.n, line.text)
        if ev:
            out.append(ev)
    return out


def _skill_terms(req: Requirement, a: _Answer, skills: Skills | None) -> list[str] | None:
    """Варианты навыка для стажа; [] — общий стаж; None — навыковое требование без
    валидного навыка (на общий стаж не откатываемся)."""
    if a.skill:
        terms = skills(a.skill) if skills else [a.skill]
        return terms if any(mentioned(t, req.text) for t in terms) else None
    return None if hints(req.text) else []


def _experience(
    req: Requirement,
    a: _Answer,
    row: MatchRow,
    doc: Document,
    facts: ResumeFacts,
    today: date,
    skills: Skills | None,
) -> bool:
    """Стаж считает код: общий по всем местам, по навыку — по местам, где навык упомянут.
    От половины требуемого — «частично», меньше — «нет» со строками дат."""
    assert req.years is not None
    terms = _skill_terms(req, a, skills)
    if terms is None:
        return False
    periods = [p for p in facts.periods if p.start is not None]
    if terms:
        periods = [p for p in periods if period_mentions(p, doc, terms)]
    months = total_months(periods, today)
    if months == 0:
        return False  # решение остаётся за моделью, «нет» — по-прежнему только с цитатой
    need = round(req.years * 12)
    row.status = (
        Status.FULL if months >= need else Status.PARTIAL if 2 * months >= need else Status.NO
    )
    row.evidence = _period_evidence(periods, doc, facts)
    row.exceeds = f"стаж {months} мес. при требуемых {need}" if months >= 2 * need else None
    row.note = f"стаж по датам: {months} мес." + (f" ({a.skill})" if terms else "")
    return True


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w{2,}", normalize(text))} - _GENERIC


def _relevant(req: Requirement, evidence: list[Evidence]) -> bool:
    """Цитата лексически относится к требованию: навык требования (с синонимами
    справочника) или общее слово."""
    names = hints(req.text)
    if names:
        return any(mentioned(n, e.quote) for n in names for e in evidence)
    words = _stems(req.text)
    return any(words & _stems(e.quote) for e in evidence)


def _row(
    req: Requirement,
    a: _Answer | None,
    doc: Document,
    facts: ResumeFacts,
    today: date,
    skills: Skills | None,
) -> MatchRow:
    row = MatchRow(requirement_id=req.id, status=Status.UNKNOWN)
    if a is None:
        row.note = "модель не дала ответа"
        return row
    evidence = _verified(a, doc, facts)
    if evidence is None:
        row.flags.append("quote_not_found")
        row.note = "цитата не найдена в резюме"
    else:
        row.status, row.evidence, row.note = a.status, evidence, a.note
    if (
        row.status == Status.NO
        and row.evidence
        and not any(NEGATION.search(e.quote) for e in row.evidence)
    ):
        row.status = Status.UNKNOWN
        row.flags.append("no_without_negation")
    positive = row.status in (Status.FULL, Status.PARTIAL)
    if positive and req.kind in (ReqKind.SKILL, ReqKind.TOOL) and not _relevant(req, row.evidence):
        row.status = Status.PARTIAL
        row.flags.append("check_relevance")
    ex = None
    if evidence is not None and a.exceeds:
        ex = check(doc, facts, a.exceeds.line, a.exceeds.quote)
        row.exceeds = a.exceeds.why if ex else None
    by_dates = bool(req.kind == ReqKind.EXPERIENCE and req.years)
    by_dates = by_dates and _experience(req, a, row, doc, facts, today, skills)
    if by_dates:  # стаж посчитал код, цитата и статус модели не нужны
        row.flags = [f for f in row.flags if f not in ("quote_not_found", "no_without_negation")]
    if not row.evidence:
        row.status = Status.UNKNOWN  # правило 1–2: ни «нет», ни «да» без проверенной цитаты
    only_letter = req.kind in (ReqKind.SKILL, ReqKind.QUALITY) and all(
        e.source == "letter" for e in row.evidence
    )
    self_claim = not by_dates and (
        a.self_claim
        or only_letter
        or all(ln.block in SELF_BLOCKS for e in row.evidence for ln in doc.lines if ln.n == e.line)
    )
    if self_claim and row.status in (Status.FULL, Status.PARTIAL):
        row.status = Status.PARTIAL
        row.flags.append("self_claim")
    if row.status != Status.FULL:
        row.exceeds = None
    elif ex and not by_dates and ex not in row.evidence:
        row.evidence.append(ex)  # цитата «сверх» — только при итоговом «полностью»
    if any(e.source == "letter" for e in row.evidence):
        row.flags.append("from_letter")
    return row


def match(
    requirements: list[Requirement],
    doc: Document,
    facts: ResumeFacts,
    llm: LLMClient,
    today: date,
    skills: Skills | None = None,
) -> list[MatchRow]:
    try:
        answers = _ask(requirements, doc, llm)
    except LLMError:  # пара не падает: всё «неизвестно», рекрутер видит флаг
        return [
            MatchRow(
                requirement_id=r.id,
                status=Status.UNKNOWN,
                flags=["llm_error"],
                note="ошибка модели",
            )
            for r in requirements
        ]
    failed = [
        r for r in requirements if r.id in answers and _verified(answers[r.id], doc, facts) is None
    ]
    if failed:
        bad = "; ".join(
            f"{r.id}: " + ", ".join(f"L{q.line} «{q.quote}»" for q in answers[r.id].evidence)
            for r in failed
        )
        hint = (
            f"В прошлом ответе цитаты не нашлись в указанных строках: {bad}. "
            "Проверь номера строк и процитируй дословно; нет подтверждения — статус unknown."
        )
        try:
            answers |= _ask(failed, doc, llm, hint)
        except LLMError:
            pass  # первые ответы остаются: непроверенные цитаты дадут «неизвестно»
    return [_row(r, answers.get(r.id), doc, facts, today, skills) for r in requirements]
