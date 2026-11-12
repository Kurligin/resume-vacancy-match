from datetime import date
from pathlib import Path

import pytest

from rvm.llm import LLMClient
from rvm.llm.fake import DemoFake
from rvm.models import Document, Line, Status
from rvm.pipeline.analyze import ReportStatus, analyze


def doc(name: str, *texts: str) -> Document:
    return Document(name=name, lines=[Line(n=i, text=t) for i, t in enumerate(texts, 1)])


def test_demo_fake_finds_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")
    llm = LLMClient(
        DemoFake(), base_url="https://fake.invalid", model="demo", cache_path=tmp_path / "c"
    )
    vac = doc(
        "вакансия.txt",
        "Аналитик данных",
        "Требования:",
        "Уверенное знание Python и pandas",
        "Опыт работы с PostgreSQL",
        "Знание немецкого языка",
    )
    res = doc("резюме.txt", "Аналитик", "Пишу отчёты на Python, pandas каждый день", "SQL")
    report = analyze(res, {"v1": vac}, llm, date(2026, 11, 12))
    assert report.status == ReportStatus.OK
    rows = {r.status for r in report.vacancies[0].rows}
    assert Status.FULL in rows and Status.UNKNOWN in rows
    full = next(r for r in report.vacancies[0].rows if r.status == Status.FULL)
    assert full.evidence and full.evidence[0].quote in res.lines[1].text
