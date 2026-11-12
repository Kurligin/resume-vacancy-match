import json
import os
from datetime import date
from pathlib import Path

import pytest
from fake_llm import FakeOpenAI

from rvm.experience import (
    check_declared,
    parse_dates,
    parse_duration,
    skill_months,
    total_months,
)
from rvm.facts import Period, ResumeFacts
from rvm.ingest import load_path
from rvm.llm import LLMClient
from rvm.models import Document, Line
from rvm.pipeline.extract_resume import extract_resume_facts

TODAY = date(2026, 11, 1)
DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")


@pytest.mark.parametrize(
    "raw, start, end, current",
    [
        ("Янв 2024 – сейчас", date(2024, 1, 1), None, True),
        ("Май 2025 — Август 2026", date(2025, 5, 1), date(2026, 8, 1), False),
        ("сентябрь 2021 — февраль 2023", date(2021, 9, 1), date(2023, 2, 1), False),
        ("03.2022 – 05.2025", date(2022, 3, 1), date(2025, 5, 1), False),
        ("Москва, март 2023 — настоящее время", date(2023, 3, 1), None, True),
        ("ноября 2019 - по н.в.", date(2019, 11, 1), None, True),
        ("2018 — 2021", date(2018, 1, 1), date(2021, 12, 1), False),
        ("2021", date(2021, 1, 1), date(2021, 12, 1), False),
        ("с мая 2020", date(2020, 5, 1), None, True),
        ("дек. 2018 — янв. 2020", date(2018, 12, 1), date(2020, 1, 1), False),
    ],
)
def test_parse_dates(raw: str, start: date, end: date | None, current: bool) -> None:
    assert parse_dates(raw) == (start, end, current)


@pytest.mark.parametrize("raw", ["", "давно", "сейчас — 2020", "2023 — 2020"])
def test_parse_dates_bad(raw: str) -> None:
    assert parse_dates(raw) is None


@pytest.mark.parametrize(
    "raw, months",
    [
        ("2 года 10 мес", 34),
        ("Опыт работы —7 лет 9 месяцев", 93),
        ("(2 года)", 24),
        ("(1,5 года)", 18),
        ("10 месяцев", 10),
        ("1 год", 12),
        ("без опыта", None),
        (None, None),
    ],
)
def test_parse_duration(raw: str | None, months: int | None) -> None:
    assert parse_duration(raw) == months


def period(dates: str, declared: str | None = None, lines: tuple[int, int] = (1, 1)) -> Period:
    parsed = parse_dates(dates)
    assert parsed
    start, end, current = parsed
    return Period(
        line_from=lines[0],
        line_to=lines[1],
        dates_raw=dates,
        start=start,
        end=end,
        current=current,
        declared_raw=declared,
        declared_months=parse_duration(declared),
    )


def facts(periods: list[Period], declared_total: str | None = None) -> ResumeFacts:
    return ResumeFacts(
        periods=periods,
        declared_total_raw=declared_total,
        declared_total_months=parse_duration(declared_total),
    )


PETROV = [
    ("Май 2025 — Август 2026", "1 год 4 месяца"),
    ("Март 2022 — Май 2025", "3 года 3 месяца"),
    ("Февраль 2020 — Март 2022", "2 года 2 месяца"),
    ("Декабрь 2018 — Январь 2020", "1 год 2 месяца"),
]


def test_total_merges_shared_months() -> None:
    f = facts([period(d, dec) for d, dec in PETROV], "7 лет 9 месяцев")
    assert sum(p.declared_months or 0 for p in f.periods) == 95  # простая сумма
    assert total_months(f.periods, TODAY) == 93
    assert check_declared(f, TODAY) == []


def test_overlapping_periods_not_doubled() -> None:
    ps = [period("Янв 2024 – сейчас"), period("Янв 2024 – сейчас"), period("Май 2026 – Авг 2026")]
    assert total_months(ps, TODAY) == 35


def test_future_end_clipped_by_today() -> None:
    assert total_months([period("01.2026 – 12.2027")], TODAY) == 11


def test_declared_mismatch() -> None:
    f = facts(
        [period("март 2023 — настоящее время", "(2 года)"), period("сентябрь 2021 — февраль 2023")]
    )
    found = check_declared(f, TODAY)
    assert [(d.period, d.declared, d.computed) for d in found] == [(0, 24, 45)]


def test_declared_total_mismatch() -> None:
    f = facts([period("Янв 2024 – сейчас", "2 года 10 мес")], "5 лет")
    [d] = check_declared(f, TODAY)
    assert (d.period, d.declared, d.computed) == (None, 60, 35)


def test_skill_months() -> None:
    doc = Document(
        name="r",
        lines=[
            Line(n=1, text="Май 2025 — Август 2026"),
            Line(n=2, text="Стек: Vue 3, Nuxt"),
            Line(n=3, text="Март 2022 — Май 2025"),
            Line(n=4, text="Стек: VUE 2, Jest"),
            Line(n=5, text="Февраль 2020 — Март 2022"),
            Line(n=6, text="HTML, CSS"),
        ],
    )
    ps = [
        period("Май 2025 — Август 2026", lines=(1, 2)),
        period("Март 2022 — Май 2025", lines=(3, 4)),
        period("Февраль 2020 — Март 2022", lines=(5, 6)),
    ]
    f = facts(ps)
    assert skill_months("vue", f, doc, TODAY) == 54
    assert skill_months("Jest", f, doc, TODAY) == 39
    assert skill_months("Go", f, doc, TODAY) == 0


def markup(lines: tuple[int, int], dates: str, declared: str | None = None) -> dict:
    return {
        "org": None,
        "title": None,
        "line_from": lines[0],
        "line_to": lines[1],
        "dates_raw": dates,
        "declared_raw": declared,
    }


def run(doc: Document, reply: dict, tmp_path: Path) -> ResumeFacts:
    fake = FakeOpenAI([json.dumps(reply, ensure_ascii=False)])
    llm = LLMClient(fake, base_url="http://x", model="m", mode="live", cache_path=tmp_path / "c")
    return extract_resume_facts(doc, llm)


def reply(periods: list[dict], **kw: object) -> dict:
    base = {
        "periods": periods,
        "education": [],
        "city": None,
        "work_formats": [],
        "declared_total_raw": None,
        "letter_lines": [],
    }
    return base | kw


def test_extract_synthetic(tmp_path: Path) -> None:
    text = [
        "Проживает: Казань",
        "Сопроводительное письмо",
        "Хочу к вам",
        "ООО Ромашка, март 2023 — настоящее время (2 года)",
        "Когда-то давно",
        "Бакалавр, 2021",
    ]
    doc = Document(name="r", lines=[Line(n=i + 1, text=t) for i, t in enumerate(text)])
    edu = {"level": "бакалавр", "field": None, "institution": None, "year": 2021, "finished": True}
    f = run(
        doc,
        reply(
            [
                markup((4, 4), "март 2023 — настоящее время", "(2 года)"),
                markup((5, 5), "Когда-то давно"),
                markup((99, 99), "май 2020 — июнь 2020"),
            ],
            education=[edu],
            city="Казань",
            work_formats=["удалённо"],
            letter_lines=[3, 99],
        ),
        tmp_path,
    )
    p = f.periods[0]
    assert (p.start, p.end, p.current, p.declared_months) == (date(2023, 3, 1), None, True, 24)
    assert f.periods[1].start is None
    assert f.letter_lines == [3]
    assert f.city == "Казань" and f.education[0].year == 2021
    assert any("Когда-то давно" in w for w in f.warnings)
    assert any("май 2020" in w for w in f.warnings)  # дат нет в указанных строках
    assert len(f.warnings) == 2


# --- реальные резюме кейса: разметку даёт «модель», номера строк ищем по тексту ---


def find(doc: Document, needle: str) -> int:
    return next(ln.n for ln in doc.lines if needle in ln.text)


def case_doc(name: str) -> Document:
    assert DATA_DIR
    [path] = Path(DATA_DIR).glob(f"*{name}*")
    return load_path(path)


@needs_data
def test_case_petrov(tmp_path: Path) -> None:
    doc = case_doc("Петров Иван")
    starts = [find(doc, d) for d, _ in PETROV]
    ends = [n - 1 for n in starts[1:]] + [find(doc, "Образование") - 1]
    periods = [markup((s, e), d, doc.lines[s].text) for s, e, (d, _) in zip(starts, ends, PETROV)]
    total = doc.lines[find(doc, "Опыт работы —") - 1].text
    f = run(doc, reply(periods, declared_total_raw=total), tmp_path)
    assert f.warnings == []
    assert [p.declared_months for p in f.periods] == [16, 39, 26, 14]
    assert total_months(f.periods, TODAY) == 93
    assert check_declared(f, TODAY) == []
    assert skill_months("Nuxt", f, doc, TODAY) == 16


@needs_data
def test_case_semenov(tmp_path: Path) -> None:
    doc = case_doc("Семенов")
    a, b = find(doc, "март 2023"), find(doc, "сентябрь 2021")
    periods = [
        markup((a - 1, b - 2), "март 2023 — настоящее время", "(2 года)"),
        markup((b - 1, b + 7), "сентябрь 2021 — февраль 2023", "(1,5 года)"),
    ]
    f = run(doc, reply(periods), tmp_path)
    assert f.warnings == []
    [d] = check_declared(f, TODAY)
    assert (d.period, d.declared, d.computed) == (0, 24, 45)


@needs_data
def test_case_ivanov_overlap(tmp_path: Path) -> None:
    doc = case_doc("Иванов")
    first = find(doc, "Янв 2024")
    second = next(ln.n for ln in doc.lines if ln.n > first and "Янв 2024" in ln.text)
    third = find(doc, "Май 2026")
    periods = [
        markup((first, second - 1), "Янв 2024 – сейчас", "2 года 10 мес"),
        markup((second, third - 1), "Янв 2024 – сейчас", "2 года 10 мес"),
        markup((third, third + 9), "Май 2026 – Авг 2026", "4 мес"),
    ]
    f = run(doc, reply(periods, declared_total_raw="2 года 10 месяцев"), tmp_path)
    assert f.warnings == []
    assert total_months(f.periods, TODAY) == 35
    assert check_declared(f, TODAY) == []
