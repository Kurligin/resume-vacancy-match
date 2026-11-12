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
