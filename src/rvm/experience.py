"""Разбор дат и длительностей из текста резюме, стаж со слиянием пересечений."""

import re
from datetime import date

from rvm.facts import Discrepancy, Period, ResumeFacts
from rvm.models import Document
from rvm.refs.skills import mentioned

MONTH = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "мае": 5, "июн": 6}
MONTH |= {"июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
EN = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH |= {m: i for i, m in enumerate(EN, 1)}
# английские — только настоящие написания: «Marketing 2020» не март
EN_FULL = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
EN_FULL += r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
TOKEN = re.compile(
    r"\b(?P<mon>(?:янв|фев|мар|апр|ма[йяе]|июн|июл|авг|сен|окт|ноя|дек)[а-яё]*"
    rf"|(?:{EN_FULL})\b)\.?\s*(?P<y1>\d{{4}})"
    r"|\b(?P<mm>\d{1,2})[./](?P<y2>\d{4})\b"
    r"|\b(?P<y3>(?:19|20)\d{2})\b"
    r"|(?P<now>сейчас|настоящ|н\.\s*в\.?|наст\.\s*вр|текущ|сих\s+пор|сей\s+день"
    r"|\bpresent\b|\bnow\b|\bcurrent)",
    re.IGNORECASE,
)
SINCE = re.compile(r"^\s*с\b", re.IGNORECASE)  # «с мая 2020» — по настоящее время
# после единственной даты — тире или «по …»: конец есть, но не разобран
DANGLING = re.compile(r"^\s*(?:[-‐-―−]|по\b|до\b|to\b|till\b|until\b)", re.IGNORECASE)
YEARS = re.compile(r"(\d+(?:[.,]\d+)?)\+?\s*(?:год|лет|years?\b|yrs?\b)", re.IGNORECASE)
MONTHS = re.compile(r"(\d+)\+?\s*(?:мес|months?\b)", re.IGNORECASE)
HALF_YEAR = re.compile(r"(?<!\w)пол\s*года(?!\w)", re.IGNORECASE)
ONE_YEAR = re.compile(r"(?<!\w)(?:год|year)(?!\w)", re.IGNORECASE)  # «год и 3 месяца»
GENITIVE = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
TOLERANCE = 2  # месяцев: округления в резюме обычны


def _token(m: re.Match[str]) -> tuple[int, int | None] | None:
    """(год, месяц | None, если указан только год); None — «по настоящее время»."""
    if m["now"]:
        return None
    if m["mon"]:
        return int(m["y1"]), MONTH[m["mon"].lower()[:3]]
    if m["mm"]:
        return int(m["y2"]), int(m["mm"])
    return int(m["y3"]), None


def parse_dates(raw: str) -> tuple[date, date | None, bool] | None:
    """«март 2023 — сейчас» → (начало, конец | None, по настоящее время); None — не разобрано."""
    found = list(TOKEN.finditer(raw))
    tokens = [_token(m) for m in found]
    if not tokens or tokens[0] is None or len(tokens) > 2:
        return None
    if len(tokens) == 1 and DANGLING.match(raw[found[0].end() :]):
        return None  # не закрывать период тем же месяцем
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
    """«2 года 10 мес», «(1,5 года)», «5+ лет», «полгода» → месяцы; None — длительности нет."""
    if not raw:
        return None
    years, months = YEARS.search(raw), MONTHS.search(raw)
    if years:
        total = round(float(years[1].replace(",", ".")) * 12)
    elif HALF_YEAR.search(raw):
        total = 6
    elif ONE_YEAR.search(raw):
        total = 12
    elif not months:
        return None
    else:
        total = 0
    return total + (int(months[1]) if months else 0)


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


def _gaps(facts: ResumeFacts, today: date) -> list[tuple[int | None, int, int, bool]]:
    """Расхождения больше TOLERANCE: (место | None — общий стаж, заявлено, по датам, текущее)."""
    out = []
    for i, p in enumerate(facts.periods):
        if p.declared_months is not None and p.start is not None:
            out.append((i, p.declared_months, total_months([p], today), p.current))
    declared = facts.declared_total_months
    if declared is not None and any(p.start for p in facts.periods):
        current = any(p.current for p in facts.periods if p.start)
        out.append((None, declared, total_months(facts.periods, today), current))
    return [g for g in out if abs(g[2] - g[1]) > TOLERANCE]


def check_declared(facts: ResumeFacts, today: date) -> list[Discrepancy]:
    """Расхождения «заявлено vs по датам». У текущих мест «заявлено меньше» — не расхождение:
    резюме написано раньше (см. snapshot_date)."""
    return [
        Discrepancy(period=i, declared=d, computed=c)
        for i, d, c, current in _gaps(facts, today)
        if not (current and d < c)
    ]


def snapshot_date(facts: ResumeFacts, today: date) -> date | None:
    """Оценка даты резюме: у текущих мест заявлено меньше посчитанного на разницу месяцев."""
    lags = [c - d for _, d, c, current in _gaps(facts, today) if current and d < c]
    if not lags:
        return None
    n = _index(today) - min(lags)
    return date(n // 12, n % 12 + 1, 1)


def snapshot_note(facts: ResumeFacts, today: date) -> str | None:
    """«резюме, похоже, от февраля 2025» или None."""
    d = snapshot_date(facts, today)
    return d and f"резюме, похоже, от {GENITIVE.split()[d.month - 1]} {d.year}"
