import json
import os
from datetime import date
from pathlib import Path

import pytest
from fake_llm import FakeOpenAI

from rvm.experience import parse_dates
from rvm.facts import Period, ResumeFacts
from rvm.ingest import load_path
from rvm.llm import LLMClient
from rvm.models import Document, Evidence, Line, MatchRow, ReqKind, Requirement, Section, Status
from rvm.pipeline import match as match_mod
from rvm.pipeline.match import match
from rvm.pipeline.verify import check, quote_ok, verified_share
from rvm.refs.skills import mentioned

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")
TODAY = date(2026, 11, 12)
LETTER = "Сопроводительное письмо"

LINES = [
    ("Иванов Иван", None),
    (LETTER, LETTER),
    ("Vue знаю, с Tailwind тоже пока не работал.", LETTER),
    ("Опыт работы", "Опыт работы"),
    ("ООО Ромашка, март 2025 — настоящее время", "Опыт работы"),
    ("Разработка на Vue 3 и Nuxt 3 (SSR), руководил командой из трёх человек.", "Опыт работы"),
    ("Обо мне", "Обо мне"),
    ("Внимателен к деталям, ответственный.", "Обо мне"),
]
DOC = Document(name="r", lines=[Line(n=i + 1, text=t, block=b) for i, (t, b) in enumerate(LINES)])
FACTS = ResumeFacts(
    periods=[
        Period(
            line_from=5,
            line_to=6,
            dates_raw="март 2025 — настоящее время",
            start=date(2025, 3, 1),
            current=True,
        )
    ]
)


def req(
    rid: str, text: str, kind: ReqKind = ReqKind.SKILL, years: float | None = None
) -> Requirement:
    return Requirement(id=rid, text=text, section=Section.MUST, kind=kind, years=years)


def ans(rid: str, status: str, ev: list[tuple[int, str]] = (), **kw: object) -> dict:
    base = {
        "requirement_id": rid,
        "status": status,
        "evidence": [{"line": n, "quote": q} for n, q in ev],
        "exceeds": None,
        "note": "",
        "self_claim": False,
        "skill": None,
    }
    return base | kw


def run(
    reqs: list[Requirement],
    replies: list[list[dict]],
    tmp_path: Path,
    doc: Document = DOC,
    facts: ResumeFacts = FACTS,
) -> tuple[dict[str, MatchRow], FakeOpenAI]:
    fake = FakeOpenAI([json.dumps({"items": r}, ensure_ascii=False) for r in replies])
    llm = LLMClient(fake, base_url="http://x", model="m", mode="live", cache_path=tmp_path / "c")
    rows = match(reqs, doc, facts, llm, TODAY)
    assert [r.requirement_id for r in rows] == [r.id for r in reqs]
    return {r.requirement_id: r for r in rows}, fake


def test_fabricated_quote_retried_then_unknown(tmp_path: Path) -> None:
    reqs = [req("R1", "React"), req("R2", "Vue")]
    first = [ans("R1", "full", [(6, "React 18 и Redux")]), ans("R2", "full", [(6, "Vue 3")])]
    retry = [ans("R1", "full", [(5, "React")])]
    rows, fake = run(reqs, [first, retry], tmp_path)
    assert len(fake.calls) == 2
    retry_prompt = fake.calls[1]["messages"][-1]["content"]
    assert "R1" in retry_prompt and "R2:" not in retry_prompt  # повтор только по провалившимся
    assert rows["R1"].status == Status.UNKNOWN and "quote_not_found" in rows["R1"].flags
    assert rows["R1"].evidence == []
    assert rows["R2"].status == Status.FULL and rows["R2"].flags == []


def test_retry_fixes_quote(tmp_path: Path) -> None:
    first = [ans("R1", "full", [(5, "Vue 3")])]  # не та строка
    retry = [ans("R1", "full", [(6, "Vue 3")])]
    rows, fake = run([req("R1", "Vue")], [first, retry], tmp_path)
    assert rows["R1"].status == Status.FULL and rows["R1"].evidence[0].line == 6
    assert len(fake.calls) == 2


def test_no_without_quote_is_unknown(tmp_path: Path) -> None:
    rows, fake = run([req("R1", "Go")], [[ans("R1", "no")]], tmp_path)
    assert rows["R1"].status == Status.UNKNOWN
    assert len(fake.calls) == 1


def test_positive_without_quote_is_unknown(tmp_path: Path) -> None:
    rows, _ = run([req("R1", "Go")], [[ans("R1", "full")]], tmp_path)
    assert rows["R1"].status == Status.UNKNOWN


def test_missing_answer_is_unknown(tmp_path: Path) -> None:
    rows, _ = run([req("R1", "Go"), req("R2", "Vue")], [[ans("R1", "unknown")]], tmp_path)
    assert rows["R2"].status == Status.UNKNOWN


def test_no_with_letter_quote_stays_no(tmp_path: Path) -> None:
    quote = "с Tailwind тоже пока не работал"
    rows, _ = run([req("R1", "Tailwind CSS")], [[ans("R1", "no", [(3, quote)])]], tmp_path)
    row = rows["R1"]
    assert row.status == Status.NO
    assert row.evidence == [Evidence(line=3, quote=quote, source="letter")]
    assert row.flags == ["from_letter"]


def test_self_claim_capped_to_partial(tmp_path: Path) -> None:
    reqs = [req("R1", "внимательность", ReqKind.QUALITY), req("R2", "ответственность")]
    reply = [
        ans("R1", "full", [(8, "Внимателен к деталям")]),  # блок «Обо мне»
        ans("R2", "full", [(8, "ответственный")], self_claim=True),
    ]
    rows, _ = run(reqs, [reply], tmp_path)
    for rid in ("R1", "R2"):
        assert rows[rid].status == Status.PARTIAL and "self_claim" in rows[rid].flags


def test_exceeds_needs_full_and_quote(tmp_path: Path) -> None:
    reqs = [req("R1", "Vue"), req("R2", "Nuxt"), req("R3", "SSR"), req("R4", "команда")]
    good = {"why": "Vue 3 и Nuxt", "line": 6, "quote": "Vue 3 и Nuxt 3 (SSR)"}
    reply = [
        ans("R1", "full", [(6, "Vue 3")], exceeds=good),
        ans("R2", "full", [(6, "Nuxt 3")], exceeds=good | {"quote": "Nuxt 4 и Angular"}),
        ans("R3", "partial", [(6, "SSR")], exceeds=good),
        ans("R4", "full", [(6, "руководил командой")], exceeds=good | {"quote": ""}),
    ]
    rows, fake = run(reqs, [reply], tmp_path)
    assert len(fake.calls) == 1  # провал цитаты «сверх» не повод для повтора
    assert rows["R1"].exceeds == "Vue 3 и Nuxt"
    assert Evidence(line=6, quote="Vue 3 и Nuxt 3 (SSR)") in rows["R1"].evidence
    for rid in ("R2", "R3", "R4"):
        assert rows[rid].exceeds is None
    assert rows["R2"].status == Status.FULL and rows["R3"].status == Status.PARTIAL


def test_experience_by_dates(tmp_path: Path) -> None:
    # с марта 2025 по ноябрь 2026 — 21 месяц
    reqs = [
        req("R1", "опыт от 1 года", ReqKind.EXPERIENCE, 1),
        req("R2", "опыт от 3 лет", ReqKind.EXPERIENCE, 3),
        req("R3", "Vue от 10 мес.", ReqKind.EXPERIENCE, 10 / 12),
        req("R4", "React от 1 года", ReqKind.EXPERIENCE, 1),
    ]
    reply = [
        ans("R1", "unknown"),
        ans("R2", "full", [(5, "март 2025")]),
        ans("R3", "partial", skill="Vue"),
        ans("R4", "unknown", skill="React"),
    ]
    rows, _ = run(reqs, [reply], tmp_path)
    assert rows["R1"].status == Status.FULL and rows["R1"].exceeds is None
    assert rows["R1"].evidence[0].line == 5
    assert rows["R2"].status == Status.PARTIAL
    assert rows["R3"].status == Status.FULL and rows["R3"].exceeds  # 21 ≥ 2 × 10
    assert rows["R4"].status == Status.UNKNOWN  # навыка нет в местах работы — решает модель


def test_skills_variants_hook(tmp_path: Path) -> None:
    reqs = [req("R1", "VueJS от 1 года", ReqKind.EXPERIENCE, 1)]
    fake = FakeOpenAI([json.dumps({"items": [ans("R1", "unknown", skill="VueJS")]})])
    llm = LLMClient(fake, base_url="http://x", model="m", mode="live", cache_path=tmp_path / "c")
    [row] = match(reqs, DOC, FACTS, llm, TODAY, skills=lambda t: [t, "Vue"])
    assert row.status == Status.FULL


def test_ocr_typo_threshold() -> None:
    line = Line(n=1, text="Стек: Taiiwind CSS", ocr=True)
    assert quote_ok(line, "Tailwind")
    assert not quote_ok(line.model_copy(update={"ocr": False}), "Tailwind")


def test_quote_normalization_and_long_quote() -> None:
    line = Line(n=1, text="ООО «Ромашка» — Vue-разработчик")
    assert quote_ok(line, 'ооо "ромашка" - vue-разработчик')
    assert not quote_ok(line, "ООО «Ромашка» — Vue-разработчик, 10 лет руководил отделом")
    assert not quote_ok(line, "  ")


def test_verified_share() -> None:
    rows = [
        MatchRow(requirement_id="R1", status=Status.FULL, evidence=[Evidence(line=1, quote="a")]),
        MatchRow(requirement_id="R2", status=Status.UNKNOWN, flags=["quote_not_found"]),
        MatchRow(requirement_id="R3", status=Status.UNKNOWN),
    ]
    assert verified_share(rows) == 0.5
    assert verified_share([]) == 1.0


# --- резюме кейса: ответ даёт «модель», номера строк ищем по тексту ---


def case_doc(name: str) -> Document:
    assert DATA_DIR
    [path] = Path(DATA_DIR).glob(f"*{name}*")
    return load_path(path)


def find(doc: Document, needle: str) -> int:
    return next(ln.n for ln in doc.lines if needle in ln.text)


@needs_data
def test_case_ivanov_tailwind_no(tmp_path: Path) -> None:
    doc = case_doc("Иванов")
    quote = "с Tailwind тоже пока не работал"
    reply = [ans("R1", "no", [(find(doc, quote), quote)])]
    rows, _ = run([req("R1", "Tailwind CSS")], [reply], tmp_path, doc, ResumeFacts())
    assert rows["R1"].status == Status.NO and rows["R1"].flags == ["from_letter"]


@needs_data
def test_case_semenov_experience(tmp_path: Path) -> None:
    doc = case_doc("Семенов")
    periods = []
    for raw, end_needle in [
        ("март 2023 — настоящее время", "HTML-верстальщик"),
        ("сентябрь 2021 — февраль 2023", "Учебные/пет-проекты"),
    ]:
        start, end, current = parse_dates(raw) or (None, None, False)
        n = find(doc, raw)
        periods.append(
            Period(
                line_from=n - 1,
                line_to=find(doc, end_needle) - 1,
                dates_raw=raw,
                start=start,
                end=end,
                current=current,
            )
        )
    reqs = [req("R1", "Опыт работы от 1 года", ReqKind.EXPERIENCE, 1)]
    rows, _ = run(reqs, [[ans("R1", "unknown")]], tmp_path, doc, ResumeFacts(periods=periods))
    row = rows["R1"]
    assert row.status == Status.FULL
    assert {e.line for e in row.evidence} == {find(doc, "март 2023"), find(doc, "сентябрь 2021")}


def test_experience_by_dates_despite_bad_quote(tmp_path: Path) -> None:
    reqs = [req("R1", "опыт от 1 года", ReqKind.EXPERIENCE, 1)]
    bad = [ans("R1", "full", [(5, "5 лет в Google")], self_claim=True)]
    rows, _ = run(reqs, [bad, bad], tmp_path)
    assert rows["R1"].status == Status.FULL and rows["R1"].flags == []


# --- R1d: строгость проверки ---


def test_no_without_negation_is_unknown(tmp_path: Path) -> None:
    rows, _ = run([req("R1", "React")], [[ans("R1", "no", [(6, "Vue 3")])]], tmp_path)
    assert rows["R1"].status == Status.UNKNOWN and "no_without_negation" in rows["R1"].flags


def test_short_quote_rejected() -> None:
    line = Line(n=1, text="Разработка на Vue 3 и Nuxt 3")
    assert not quote_ok(line, "а")
    assert not quote_ok(line, "Ра")
    assert not quote_ok(line, "зработка")  # не целое слово
    assert quote_ok(line, "Vue 3")


def test_short_quote_of_known_skill() -> None:
    assert quote_ok(Line(n=1, text="Уверенное владение 1С:Предприятие"), "1С")
    assert quote_ok(Line(n=1, text="Бэкенд на Go и Python"), "Go")
    assert not quote_ok(Line(n=1, text="Пишу на Golang"), "Go")  # короткая — только точно


def test_evidence_quote_is_line_text() -> None:
    ev = check(DOC, FACTS, 6, "разработка на vue 3 и nuxt 3 (ssr)")
    assert ev and ev.quote == "Разработка на Vue 3 и Nuxt 3 (SSR)"
    ocr = Document(name="o", lines=[Line(n=1, text="Стек: Taiiwind CSS, Vue", ocr=True)])
    ev = check(ocr, ResumeFacts(), 1, "Tailwind CSS")
    assert ev and ev.quote == "Taiiwind CSS"
    # выдумка длиннее строки не проходит даже нечётко
    assert check(DOC, FACTS, 8, "Внимателен к деталям, ответственный. Лидер команды.") is None


def test_skill_mention_word_boundaries() -> None:
    assert not mentioned("Go", "Google Cloud, Google Analytics")
    assert mentioned("Go", "Бэкенд на Go и Python")
    assert mentioned("golang", "Бэкенд на Go")
    assert mentioned("C++", "Языки: C++, Python")
    assert not mentioned("Java", "JavaScript")


def test_skill_experience_not_found_in_google(tmp_path: Path) -> None:
    doc = Document(
        name="g",
        lines=[
            Line(n=1, text="ООО Google, март 2025 — настоящее время"),
            Line(n=2, text="Google Ads"),
        ],
    )
    facts = ResumeFacts(
        periods=[
            Period(
                line_from=1,
                line_to=2,
                dates_raw="март 2025 — настоящее время",
                start=date(2025, 3, 1),
                current=True,
            )
        ]
    )
    reqs = [req("R1", "Go от 1 года", ReqKind.EXPERIENCE, 1)]
    rows, _ = run(reqs, [[ans("R1", "unknown", skill="Go")]], tmp_path, doc, facts)
    assert rows["R1"].status == Status.UNKNOWN


def test_skill_requirement_without_valid_skill_not_general(tmp_path: Path) -> None:
    reqs = [
        req("R1", "Vue от 1 года", ReqKind.EXPERIENCE, 1),  # навык модели не из требования
        req("R2", "Vue от 1 года", ReqKind.EXPERIENCE, 1),  # навык не назван
    ]
    reply = [ans("R1", "unknown", skill="Angular"), ans("R2", "unknown")]
    rows, _ = run(reqs, [reply], tmp_path)
    assert rows["R1"].status == Status.UNKNOWN and rows["R2"].status == Status.UNKNOWN


def test_experience_below_half_is_no(tmp_path: Path) -> None:
    facts = ResumeFacts(
        periods=[
            Period(
                line_from=5,
                line_to=6,
                dates_raw="ноябрь 2026",
                start=date(2026, 11, 1),
                current=True,
            )
        ]
    )
    reqs = [
        req("R1", "опыт от 3 лет", ReqKind.EXPERIENCE, 3),
        req("R2", "опыт от 2 мес.", ReqKind.EXPERIENCE, 2 / 12),
    ]
    rows, _ = run(reqs, [[ans("R1", "unknown"), ans("R2", "unknown")]], tmp_path, facts=facts)
    assert rows["R1"].status == Status.NO and rows["R1"].evidence[0].line == 5
    assert rows["R2"].status == Status.PARTIAL  # 1 из 2 мес. — ровно половина


def test_irrelevant_quote_flagged(tmp_path: Path) -> None:
    reqs = [req("R1", "Tailwind CSS"), req("R2", "Vue.js", ReqKind.TOOL)]
    reply = [ans("R1", "full", [(6, "Vue 3")]), ans("R2", "full", [(6, "Vue 3")])]
    rows, _ = run(reqs, [reply], tmp_path)
    assert rows["R1"].status == Status.PARTIAL and "check_relevance" in rows["R1"].flags
    assert rows["R2"].status == Status.FULL and rows["R2"].flags == []


def test_letter_only_skill_is_self_claim(tmp_path: Path) -> None:
    rows, _ = run([req("R1", "Vue")], [[ans("R1", "full", [(3, "Vue знаю")])]], tmp_path)
    assert rows["R1"].status == Status.PARTIAL and "self_claim" in rows["R1"].flags


def test_exceeds_quote_only_when_full(tmp_path: Path) -> None:
    good = {"why": "SSR", "line": 6, "quote": "Nuxt 3 (SSR)"}
    rows, _ = run(
        [req("R1", "Nuxt")], [[ans("R1", "partial", [(6, "Nuxt 3")], exceeds=good)]], tmp_path
    )
    assert [e.quote for e in rows["R1"].evidence] == ["Nuxt 3"]


def test_llm_error_rows_unknown(tmp_path: Path) -> None:
    reqs = [req("R1", "Vue"), req("R2", "React")]
    rows, _ = run(reqs, [["not json"], ["still not json"]], tmp_path)
    for r in rows.values():
        assert r.status == Status.UNKNOWN and r.flags == ["llm_error"]


def test_only_study_projects_is_negation(tmp_path: Path) -> None:
    doc = Document(name="r", lines=[Line(n=1, text="Vue: только учебные проекты.")])
    quote = "Vue: только учебные проекты"
    reqs = [req("R1", "Коммерческий опыт на Vue", ReqKind.EXPERIENCE)]
    rows, _ = run(reqs, [[ans("R1", "no", [(1, quote)])]], tmp_path, doc, ResumeFacts())
    assert rows["R1"].status == Status.NO


# --- Q2: «нет» только по цитате, относящейся к требованию ---

NO_DOC = Document(
    name="r",
    lines=[
        Line(n=1, text="Vue знаю, но коммерческих проектов на нём у меня нет."),
        Line(n=2, text="Не готов к переезду."),
        Line(n=3, text="Формат: удалённо, гибрид, на месте работодателя."),
    ],
)


def test_no_with_unrelated_negation_is_unknown(tmp_path: Path) -> None:
    quote = "коммерческих проектов на нём у меня нет"
    reqs = [
        req("R1", "Опыт учёта ОС", ReqKind.EXPERIENCE),
        req("R2", "Коммерческий опыт Vue", ReqKind.EXPERIENCE),
        req("R3", "Очный формат в Долгопрудном", ReqKind.CONDITION),
    ]
    reply = [
        ans("R1", "no", [(1, quote)]),
        ans("R2", "no", [(1, quote)]),
        ans("R3", "no", [(2, "Не готов к переезду")]),
    ]
    rows, _ = run(reqs, [reply], tmp_path, NO_DOC, ResumeFacts())
    assert rows["R1"].status == Status.UNKNOWN and "no_unrelated_quote" in rows["R1"].flags
    assert rows["R2"].status == Status.NO and rows["R2"].flags == []
    assert rows["R3"].status == Status.UNKNOWN and "no_unrelated_quote" in rows["R3"].flags


def test_no_related_by_case_forms(tmp_path: Path) -> None:
    reqs = [req("R1", "Готовность к переезду", ReqKind.CONDITION)]
    reply = [ans("R1", "no", [(2, "Не готов к переезду")])]
    rows, _ = run(reqs, [reply], tmp_path, NO_DOC, ResumeFacts())
    assert rows["R1"].status == Status.NO


def test_match_v3_prompt_rules() -> None:
    text = match_mod.PROMPT.read_text(encoding="utf-8")
    assert match_mod.PROMPT_VERSION == "match.v3"
    assert "не готов к переезду" in text and "подтверждение" in text
