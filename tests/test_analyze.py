import json
import os
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rvm.ingest import load_path
from rvm.llm import LLMClient
from rvm.models import Document, Line, Status
from rvm.pipeline import analyze as analyze_mod
from rvm.pipeline.analyze import Report, ReportStatus, analyze, guess_applied
from rvm.privacy import PrivacyError

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")
TODAY = date(2026, 11, 12)
EXTERNAL = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")


class RoutingFake:
    """Транспорт модели: ответ по имени схемы; пишет все сообщения; fail — ломает сопоставление
    для вакансии, в требованиях которой есть эта подстрока."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        name = kwargs["response_format"]["json_schema"]["name"]
        user = kwargs["messages"][-1]["content"]
        if name == "_Items":
            reply = {
                "items": [
                    {
                        "text": f"требование {i} {user.splitlines()[0][:30]}",
                        "section": "must",
                        "kind": "skill",
                        "years": None,
                        "source_line": i,
                    }
                    for i in (1, 2)
                ]
            }
        elif name == "_Markup":
            reply = {
                "periods": [],
                "education": [],
                "city": None,
                "work_formats": [],
                "declared_total_raw": None,
                "letter_lines": [],
            }
        else:
            if self.fail and any(self.fail in m["content"] for m in kwargs["messages"]):
                return self._reply("не json")
            ids = re.findall(r"^(R\d+):", user, re.MULTILINE)
            reply = {
                "items": [
                    {
                        "requirement_id": i,
                        "status": "unknown",
                        "evidence": [],
                        "exceeds": None,
                        "note": "",
                        "self_claim": False,
                        "skill": None,
                    }
                    for i in ids
                ]
            }
        return self._reply(json.dumps(reply, ensure_ascii=False))

    @staticmethod
    def _reply(content: str) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    def sent(self) -> str:
        return "\n".join(m["content"] for c in self.calls for m in c["messages"])


def make_llm(tmp_path: Path, fake: RoutingFake, base_url: str = EXTERNAL) -> LLMClient:
    return LLMClient(
        fake, base_url=base_url, model="fake", mode="live", cache_path=tmp_path / "c.sqlite"
    )


def doc_of(name: str, *texts: str, block: str | None = None) -> Document:
    return Document(
        name=name, lines=[Line(n=i, text=t, block=block) for i, t in enumerate(texts, 1)]
    )


RESUME = doc_of(
    "Сидоров Пётр.docx",
    "Сидоров Пётр",
    "Телефон: +7 (916) 123-45-67, почта p.sidorov@mail.ru",
    "Опыт: Python 5 лет",
)
VACANCIES = {
    "v1": doc_of("Вакансия 1.docx", "Python-разработчик", "Требования: Python"),
    "v2": doc_of("Вакансия 2.docx", "Бухгалтер", "Требования: 1С"),
}


def test_report_and_no_pii_in_messages(tmp_path: Path) -> None:
    fake = RoutingFake()
    report = analyze(RESUME, VACANCIES, make_llm(tmp_path, fake), TODAY)
    assert isinstance(report, Report)
    assert report.status == ReportStatus.OK
    assert {v.id for v in report.vacancies} == {"v1", "v2"}
    assert all(len(v.rows) == 2 for v in report.vacancies)
    assert report.resume.original[0].text == "Сидоров Пётр"
    assert "Сидоров" not in report.resume.lines[0].text
    sent = fake.sent()
    for leak in ("Сидоров", "Пётр", "916", "mail.ru"):
        assert leak not in sent
    Report.model_validate_json(report.model_dump_json())  # JSON туда и обратно


def test_privacy_error_external_no_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: object) -> None:
        raise PrivacyError("после маски остались данные: {'ФИО': 1}")

    monkeypatch.setattr(analyze_mod, "assert_clean", boom)
    fake = RoutingFake()
    report = analyze(RESUME, VACANCIES, make_llm(tmp_path, fake), TODAY)
    assert fake.calls == []
    assert report.status == ReportStatus.PRIVACY_ERROR
    assert report.vacancies == []


def test_privacy_error_local_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: object) -> None:
        raise PrivacyError("после маски остались данные: {'ФИО': 1}")

    monkeypatch.setattr(analyze_mod, "assert_clean", boom)
    fake = RoutingFake()
    llm = make_llm(tmp_path, fake, base_url="http://localhost:11434/v1")
    report = analyze(RESUME, VACANCIES, llm, TODAY)
    assert fake.calls
    assert report.status == ReportStatus.OK
    assert any("обезличивани" in w for w in report.warnings)


def test_llm_error_in_one_pair(tmp_path: Path) -> None:
    fake = RoutingFake(fail="Бухгалтер")
    report = analyze(RESUME, VACANCIES, make_llm(tmp_path, fake), TODAY)
    by_id = {v.id: v for v in report.vacancies}
    assert all(r.status == Status.UNKNOWN and "llm_error" in r.flags for r in by_id["v2"].rows)
    assert not any("llm_error" in r.flags for r in by_id["v1"].rows)
    assert by_id["v1"].place is not None


def test_vacancy_with_leftover_contact_skipped(tmp_path: Path) -> None:
    vacs = {
        **VACANCIES,
        "v3": doc_of("Вакансия 3.docx", "Аналитик", "Контактное лицо: звонить 916 123 45 67"),
    }
    fake = RoutingFake()
    report = analyze(RESUME, vacs, make_llm(tmp_path, fake), TODAY)
    assert report.status == ReportStatus.OK
    assert {v.id for v in report.vacancies} == {"v1", "v2"}
    assert any("Вакансия 3.docx" in w and "остались контакты" in w for w in report.warnings)
    assert "123 45 67" not in fake.sent()
    # отклик на пропущенную вакансию — рейтинг всё равно строится
    report = analyze(RESUME, vacs, make_llm(tmp_path, fake), TODAY, applied="v3")
    assert {v.id for v in report.vacancies} == {"v1", "v2"}


def test_applied_param_and_heuristic() -> None:
    letter = doc_of(
        "r.docx",
        "Сопроводительное письмо",
        "Пишу по вакансии бухгалтера по расчёту зарплаты",
        block="Сопроводительное письмо",
    )
    vacs = {
        "a": doc_of("a.docx", "Python-разработчик"),
        "b": doc_of("b.docx", "Бухгалтер по расчёту заработной платы"),
    }
    assert guess_applied(letter, vacs) == "b"
    assert guess_applied(RESUME, vacs) is None  # ни письма, ни желаемой должности


@needs_data
def test_case_files(tmp_path: Path) -> None:
    files = sorted(Path(str(DATA_DIR)).glob("*.docx"))
    vac_files = [f for f in files if "вакансия" in f.name.lower()]
    res_files = [f for f in files if f not in vac_files]
    vacancies = {f.stem: load_path(f) for f in vac_files}
    fake = RoutingFake()
    llm = make_llm(tmp_path, fake)
    reports = {
        f.stem: analyze(load_path(f), vacancies, llm, TODAY, source_name=f.name) for f in res_files
    }
    assert len(reports) == 4
    assert all(r.status == ReportStatus.OK and len(r.vacancies) == 3 for r in reports.values())
    ivanov = next(r for k, r in reports.items() if k.startswith("Иванов"))
    assert ivanov.applied is not None
    assert vacancies[ivanov.applied].lines[0].text.startswith("Middle Frontend-разработчик")

    sent = fake.sent()
    leaks: set[str] = set()
    for f in res_files:
        leaks |= set(re.findall(r"[А-ЯЁ][а-яё]+", f.stem))  # фамилия и имя из имени файла
        text = "\n".join(ln.text for ln in load_path(f).lines)
        leaks |= set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text))
        leaks |= {re.sub(r"\D", "", p)[-7:] for p in re.findall(r"\+?\d[\d ()-]{9,}\d", text)}
    leaks -= {"резюме"}
    assert len(leaks) >= 7  # фамилии и имена четырёх кандидатов
    found = sorted(w for w in leaks if re.search(rf"(?<!\w){re.escape(w)}", sent))
    assert found == []
