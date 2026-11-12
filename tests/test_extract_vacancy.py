import json
from pathlib import Path

from fake_llm import FakeOpenAI

from rvm.llm import LLMClient
from rvm.models import Document, Line, ReqKind, Section
from rvm.pipeline.extract_vacancy import extract_requirements, line_sections

TEXT = [
    "Frontend-разработчик",
    "ТРЕБОВАНИЯ К КАНДИДАТУ:",
    "- опыт с Vue от 2 лет",
    "Будет плюсом",
    "- знание Nuxt",
    "Обязанности:",
    "- верстка интерфейсов",
    "Мы предлагаем:",
    "- удалённая работа",
]
DOC = Document(name="v.docx", lines=[Line(n=i + 1, text=t) for i, t in enumerate(TEXT)])


def test_line_sections() -> None:
    s = line_sections(DOC)
    assert s[1] is None
    assert s[3] == Section.MUST
    assert s[5] == Section.NICE
    assert s[7] == Section.INFERRED
    assert s[9] == Section.CONDITION


def test_unknown_heading_resets_section() -> None:
    doc = Document(
        name="v",
        lines=[Line(n=1, text="Требования"), Line(n=2, text="О компании"), Line(n=3, text="x")],
    )
    assert line_sections(doc)[3] is None


def item(text: str, section: str, line: int | None, kind: str = "skill") -> dict:
    return {"text": text, "section": section, "kind": kind, "years": None, "source_line": line}


def test_code_decides_section_and_ids(tmp_path: Path) -> None:
    reply = {
        "items": [
            item("Vue", "must", 3),
            item("Nuxt", "must", 5),  # модель ошиблась, строка под «Будет плюсом»
            item("опишите опыт", "nice", 99, kind="request"),  # строки 99 нет
            item("заголовок", "must", 1),  # строка вне разделов — ответ модели остаётся
        ]
    }
    fake = FakeOpenAI([json.dumps(reply, ensure_ascii=False)])
    llm = LLMClient(fake, base_url="http://x", model="m", mode="live", cache_path=tmp_path / "c")
    reqs = extract_requirements(DOC, llm)
    assert [r.id for r in reqs] == ["R1", "R2", "R3", "R4"]
    assert reqs[0].section == Section.MUST
    assert reqs[1].section == Section.NICE
    assert reqs[2].source_line is None and reqs[2].section == Section.NICE
    assert reqs[2].kind == ReqKind.REQUEST
    assert reqs[3].section == Section.MUST
    assert "[L3] - опыт с Vue от 2 лет" in fake.calls[0]["messages"][-1]["content"]


def sections_of(*texts: str) -> dict[int, Section | None]:
    return line_sections(
        Document(name="v", lines=[Line(n=i + 1, text=t) for i, t in enumerate(texts)])
    )


def test_unknown_heading_with_colon_hands_over_to_model() -> None:
    s = sections_of("Требования:", "Python", "Наш стек:", "Vue", "Ты нам подходишь, если:", "Go")
    assert s[2] == Section.MUST
    assert s[3] is None and s[4] is None
    assert s[6] == Section.MUST


def test_long_line_with_colon_is_not_heading() -> None:
    s = sections_of("Обязанности:", "Организовать список ОС, которые у нас были:", "- покупки")
    assert s[2] == Section.INFERRED and s[3] == Section.INFERRED


def test_unknown_short_line_before_bullets_is_heading() -> None:
    s = sections_of("Условия:", "- ДМС", "Наш стек", "- Vue")
    assert s[2] == Section.CONDITION
    assert s[4] is None


def test_who_we_look_for_and_plus() -> None:
    s = sections_of("О нас", "Кого мы ищем:", "Vue 3", "Будет плюсом:", "Nuxt")
    assert s[3] == Section.MUST
    assert s[5] == Section.NICE


def test_hh_key_skills_after_conditions_are_must() -> None:
    s = sections_of("Условия:", "Удалёнка", "Ключевые навыки", "Vue", "TypeScript")
    assert s[2] == Section.CONDITION
    assert s[4] == Section.MUST and s[5] == Section.MUST


def test_english_headings() -> None:
    s = sections_of(
        "Responsibilities:",
        "build UI",
        "Requirements:",
        "Vue",
        "Nice to have:",
        "Nuxt",
        "What we offer",
        "remote",
        "Must have",
        "Go",
        "Would be a plus:",
        "Rust",
    )
    assert s[2] == Section.INFERRED
    assert s[4] == Section.MUST
    assert s[6] == Section.NICE
    assert s[8] == Section.CONDITION
    assert s[10] == Section.MUST
    assert s[12] == Section.NICE


def test_more_russian_headings() -> None:
    for head, section in [
        ("Мы ждём от тебя:", Section.MUST),
        ("Необходимые навыки", Section.MUST),
        ("Что нужно:", Section.MUST),
        ("Наши ожидания", Section.MUST),
        ("Обязательно:", Section.MUST),
        ("Плюсом будет", Section.NICE),
        ("Что нужно делать:", Section.INFERRED),
    ]:
        assert sections_of("Условия:", head, "пункт")[3] == section, head


def test_item_with_keyword_is_not_heading() -> None:
    s = sections_of("Требования:", "- Vue 3", "Желательно знание Vue", "- TypeScript")
    assert s[4] == Section.MUST  # раздел не съехал в «плюс»
    assert s[3] is None  # сам пункт с «желательно» — решает модель
    s = sections_of("Требования:", "Vue 3", "Желательно знание Vue", "TypeScript")
    assert s[4] == Section.MUST


def test_keyword_heading_with_tail() -> None:
    assert sections_of("ТРЕБОВАНИЯ К КАНДИДАТУ", "Vue")[2] == Section.MUST
    assert sections_of("О нас", "Требования к кандидату", "- Vue")[3] == Section.MUST
