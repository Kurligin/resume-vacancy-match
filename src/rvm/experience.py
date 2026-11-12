"""Разбор дат и длительностей из текста резюме, стаж со слиянием пересечений."""

import re
from datetime import date

from rvm.facts import Discrepancy, Period, ResumeFacts
from rvm.models import Document
from rvm.refs.skills import mentioned

MONTH = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "мае": 5, "июн": 6}
MONTH |= {"июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
TOKEN = re.compile(
    r"\b(?P<mon>янв|фев|мар|апр|ма[йяе]|июн|июл|авг|сен|окт|ноя|дек)[а-яё]*\.?\s*(?P<y1>\d{4})"
    r"|\b(?P<mm>\d{1,2})[./](?P<y2>\d{4})\b"
    r"|\b(?P<y3>(?:19|20)\d{2})\b"
    r"|(?P<now>сейчас|настоящ|н\.\s*в\.?|наст\.\s*вр|текущ)",
    re.IGNORECASE,
)
SINCE = re.compile(r"^\s*с\b", re.IGNORECASE)  # «с мая 2020» — по настоящее время
YEARS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:год|лет)", re.IGNORECASE)
MONTHS = re.compile(r"(\d+)\s*мес", re.IGNORECASE)
TOLERANCE = 2  # месяцев: округления в резюме обычны


def _token(m: re.Match[str]) -> tuple[int, int | None] | None:
    """(год, месяц | None, если указан только год); None — «по настоящее время»."""
    if m["now"]:
        return None
    if m["mon"]:
        return int(m["y1"]), MONTH[m["mon"].lower()]
    if m["mm"]:
        return int(m["y2"]), int(m["mm"])
    return int(m["y3"]), None


def parse_dates(raw: str) -> tuple[date, date | None, bool] | None:
    """«март 2023 — сейчас» → (начало, конец | None, по настоящее время); None — не разобрано."""
    tokens = [_token(m) for m in TOKEN.finditer(raw)]
    if not tokens or tokens[0] is None or len(tokens) > 2:
        return None
    if len(tokens) == 1 and SINCE.match(raw):
        tokens.append(None)
    first, last = tokens[0], tokens[-1]
    if any(t and t[1] is not None and not 1 <= t[1] <= 12 for t in (first, last)):
        return None
    start = date(first[0], first[1] or 1, 1)
    if last is None:
        return start, None, True
    end = date(last[0], last[1] or 12, 1)  # голый год в конце — по декабрь
    return (start, end, False) if start <= end else None


def parse_duration(raw: str | None) -> int | None:
    """«2 года 10 мес», «(1,5 года)» → месяцы; None — длительности в тексте нет."""
    if not raw:
        return None
    years, months = YEARS.search(raw), MONTHS.search(raw)
    if not years and not months:
        return None
    total = float(years[1].replace(",", ".")) * 12 if years else 0
    return round(total) + (int(months[1]) if months else 0)


def _index(d: date) -> int:
    return d.year * 12 + d.month - 1


def total_months(periods: list[Period], today: date) -> int:
    """Стаж в месяцах: месяцы включительно, пересечения сливаются, будущее обрезается today."""
    now = _index(today)
    spans = sorted(
        (_index(p.start), min(now, _index(p.end) if p.end else now))
        for p in periods
        if p.start is not None
    )
    total, covered = 0, -1  # covered — последний уже учтённый месяц
    for lo, hi in spans:
        lo = max(lo, covered + 1)
        if hi >= lo:
            total += hi - lo + 1
            covered = hi
    return total


def skill_months(term: str, facts: ResumeFacts, doc: Document, today: date) -> int:
    """Стаж по местам работы, в строках которых навык упомянут целым словом (см. mentioned)."""
    return total_months([p for p in facts.periods if period_mentions(p, doc, [term])], today)


def period_mentions(p: Period, doc: Document, terms: list[str]) -> bool:
    """Хоть один вариант навыка упомянут в строках места работы."""
    lines = [ln.text for ln in doc.lines if p.line_from <= ln.n <= p.line_to]
    return any(mentioned(t, text) for t in terms for text in lines)


def check_declared(facts: ResumeFacts, today: date) -> list[Discrepancy]:
    """Расхождения «заявлено vs по датам» больше TOLERANCE месяцев: по местам и по общему стажу."""
    out = []
    for i, p in enumerate(facts.periods):
        if p.declared_months is not None and p.start is not None:
            computed = total_months([p], today)
            if abs(computed - p.declared_months) > TOLERANCE:
                out.append(Discrepancy(period=i, declared=p.declared_months, computed=computed))
    declared = facts.declared_total_months
    if declared is not None and any(p.start for p in facts.periods):
        computed = total_months(facts.periods, today)
        if abs(computed - declared) > TOLERANCE:
            out.append(Discrepancy(period=None, declared=declared, computed=computed))
    return out
