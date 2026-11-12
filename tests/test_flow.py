"""Поток откликов: поля списка, «Разбор», выборочная проверка, пакет для заказчика, вакансии с hh."""

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_api
from docx import Document as Docx
from fastapi.testclient import TestClient
from test_api import client, upload, wait_batch

from rvm import store
from rvm.api import hh
from rvm.facts import Discrepancy, Period, ResumeFacts
from rvm.models import Document, Evidence, Line, MatchRow, ReqKind, Requirement, Section, Status
from rvm.pipeline.analyze import Facts, Report, ReportStatus, ResumeView, VacancyResult
from rvm.pipeline.explain import Insights, Point
from rvm.pipeline.questions import Question, Questions
from rvm.privacy import Masked
from rvm.scoring import Verdict
from rvm.scoring.rank import Redirect
from rvm.scoring.score import Contribution, PairScore

# фикстуры с демо-сессией из test_api
_salt, fake, app, api = test_api._salt, test_api.fake, test_api.app, test_api.api

REQS = [
    Requirement(id="R1", text="Python", section=Section.MUST, kind=ReqKind.SKILL),
    Requirement(id="R2", text="SQL", section=Section.MUST, kind=ReqKind.SKILL),
    Requirement(id="R3", text="Docker", section=Section.NICE, kind=ReqKind.TOOL),
]


def result(
    vid: str, score: float, low: float, high: float, verdict: Verdict, blockers: list[str]
) -> VacancyResult:
    rows = [
        MatchRow(requirement_id="R1", status=Status.FULL, evidence=[Evidence(line=2, quote="Py")]),
        MatchRow(requirement_id="R2", status=Status.NO),
    ]
    contribs = [
        Contribution(
            requirement_id="R1",
            section=Section.MUST,
            status=Status.FULL,
            weight=3,
            gain=40,
            loss=0,
            blocker=False,
        ),
        Contribution(
            requirement_id="R2",
            section=Section.MUST,
            status=Status.NO,
            weight=3,
            gain=0,
            loss=40,
            blocker="R2" in blockers,
        ),
    ]
    ps = PairScore(
        score=score,
        low=low,
        high=high,
        must_coverage=score,
        blockers=blockers,
        exceeds_count=0,
        contributions=contribs,
    )
    return VacancyResult(
        id=vid,
        title=f"Вакансия {vid}",
        requirements=REQS,
        rows=rows,
        score=ps,
        verdict=verdict,
        place=1,
        insights=Insights(
            strengths=[Point(text="Пишет на Python пять лет", refs=["R1"])],
            risks=[Point(text="SQL не указан", refs=["R2"])],
            recommendation=Point(text="Пригласить на интервью", refs=["VERDICT"]),
        ),
        questions=Questions(
            items=[
                Question(requirement_id="R2", text="Расскажите про опыт с SQL."),
                Question(requirement_id="R2", text="Есть ли у вас дети?"),
            ]
        ),
    )


def report(
    score: float = 85,
    low: float | None = None,
    high: float | None = None,
    verdict: Verdict = Verdict.FIT,
    blockers: tuple[str, ...] = (),
    redirect: bool = False,
    disc: bool = False,
) -> Report:
    res = result("1", score, low or score - 3, high or score + 3, verdict, list(blockers))
    return Report(
        status=ReportStatus.OK,
        resume=ResumeView(name="r", lines=[], original=[], status="ok"),
        applied="2" if redirect else "1",
        applied_guessed=False,
        vacancies=[res],
        redirect=(
            Redirect(from_vacancy="2", to_vacancy="1", gap=12, reasons=[]) if redirect else None
        ),
        none_fit=False,
        facts=Facts(
            resume=ResumeFacts(
                periods=[Period(title="Инженер-программист", line_from=2, line_to=2, dates_raw="")]
            ),
            total_months=60,
            discrepancies=[Discrepancy(period=None, declared=60, computed=40)] if disc else [],
            snapshot_note=None,
        ),
        warnings=[],
        model="fake",
        prompt_versions={},
        created_at=datetime.now(UTC),
        elapsed_s=0,
    )


def seed(db: Path, i: int, rep: Report) -> int:
    lines = [Line(n=1, text=f"Резюме: Иванов Иван, {i} лет"), Line(n=2, text="Python 5 лет")]
    masked_lines = [Line(n=1, text="Резюме: [ФИО] [ФИО], [возраст]"), lines[1]]
    doc = Document(name=f"r{i}.txt", lines=lines)
    masked = Masked(
        doc=doc.model_copy(update={"lines": masked_lines}),
        replacements=[],
        contact_hashes=[],
        found={},
    )
    added = store.add_resume(db, f"резюме {i}".encode(), doc, masked)
    store.save_report(db, added.resume_id, rep)
    return added.candidate_id


@pytest.fixture
def db(tmp_path: Path, api: TestClient) -> Path:
    path = tmp_path / "rvm.sqlite"
    store.add_vacancy(path, Document(name="v", lines=[Line(n=1, text="Вакансия 1")]))
    return path


def triage(api: TestClient, **params: str) -> dict[str, Any]:
    r = api.get("/api/triage", params=params or None)
    assert r.status_code == 200, r.text
    return r.json()


def where(t: dict[str, Any]) -> dict[int, str]:
    return {it["id"]: b for b in ("need", "fit", "nofit") for it in t[b]}


def test_buckets(api: TestClient, db: Path) -> None:
    fit = seed(db, 1, report(85))
    nofit = seed(db, 2, report(20, verdict=Verdict.NO_FIT))
    near = seed(db, 3, report(72))
    wide = seed(db, 4, report(60, 30, 80, verdict=Verdict.PARTIAL))
    redir = seed(db, 5, report(90, redirect=True))
    disc = seed(db, 6, report(90, disc=True))
    blocker = seed(db, 7, report(60, verdict=Verdict.PARTIAL, blockers=("R2",)))
    middle = seed(db, 8, report(58, verdict=Verdict.PARTIAL))
    t = triage(api)
    assert where(t) == {
        fit: "fit",
        nofit: "nofit",
        near: "need",
        wide: "need",
        redir: "need",
        disc: "need",
        blocker: "nofit",
        middle: "need",
    }
    items = {it["id"]: it for b in ("need", "fit", "nofit") for it in t[b]}
    assert items[fit]["proposed"] == 1 and items[nofit]["proposed"] == 4
    assert items[redir]["proposed"] == 2 and "подходит «Вакансия 1»" in items[redir]["why"][0]
    assert "вилка" in " ".join(items[wide]["why"]) and "порог" in " ".join(items[near]["why"])
    assert "SQL" in " ".join(items[blocker]["why"])
    assert items[fit]["pros"][0] == {"text": "Пишет на Python пять лет", "lines": [2]}
    assert items[fit]["cons"][0]["text"] == "SQL не указан"
    assert items[fit]["position"] == "Инженер-программист"


def test_spot_one_in_ten(api: TestClient, db: Path) -> None:
    ids = [seed(db, i, report(20, verdict=Verdict.NO_FIT)) for i in range(10)]
    spotted = [it["id"] for it in triage(api)["nofit"] if it["spot_check"]]
    assert len(spotted) == 1
    r = api.post("/api/triage/spot", json={"candidate_id": spotted[0], "agree": False})
    assert r.status_code == 200, r.text
    t = triage(api)
    assert where(t)[spotted[0]] == "need"
    assert not any(it["spot_check"] for it in t["nofit"])  # проверка уже ответила
    events = api.get(f"/api/candidates/{spotted[0]}").json()["events"]
    assert any(e["type"] == "spot_check" and e["details"]["agree"] is False for e in events)
    assert len(ids) == 10


def test_decide_batch(app: Any, api: TestClient, db: Path) -> None:
    a, b, c = (seed(db, i, report(20, verdict=Verdict.NO_FIT)) for i in range(3))
    r = api.post("/api/triage/decide", json={"candidate_ids": [a, 999], "decision": 4})
    assert r.status_code == 404 and api.get(f"/api/candidates/{a}").json()["status"] == "new"
    r = api.post("/api/triage/decide", json={"candidate_ids": [a, b], "decision": 4})
    assert r.status_code == 200, r.text
    assert set(where(triage(api))) == {c}
    card = api.get(f"/api/candidates/{a}").json()
    assert card["status"] == "rejected"
    ev = [e for e in card["events"] if e["type"] == "status"][-1]
    assert ev["details"]["decision"] == 4 and "рекрутер" in ev["who"]
    api.post("/api/triage/decide", json={"candidate_ids": [c], "decision": 3})
    assert api.get(f"/api/candidates/{c}").json()["status"] == "reserve"
    with client(app, "manager") as m:
        assert (
            m.post("/api/triage/decide", json={"candidate_ids": [c], "decision": 1}).status_code
            == 403
        )


def test_privacy_error_needs_file_check(api: TestClient, db: Path) -> None:
    rep = report().model_copy(
        update={"status": ReportStatus.PRIVACY_ERROR, "vacancies": [], "error": "обезличивание"}
    )
    seed(db, 1, rep)
    t = triage(api)
    assert t["check_file"] == 1 and t["pending"] == 0 and where(t) == {}


def test_triage_by_batch(api: TestClient, db: Path) -> None:
    assert api.get("/api/triage", params={"batch": "нет"}).status_code == 404
    other = seed(db, 1, report(85))
    bid = upload(api, [("a.txt", "Кандидат\nPython 3 года\n".encode())])
    cid = wait_batch(api, bid)[0]["candidate_id"]
    assert set(where(triage(api, batch=bid))) == {cid}
    assert {cid, other} <= set(where(triage(api)))  # без пачки — все без решения


def test_candidate_fields_and_reply(api: TestClient, db: Path) -> None:
    cid = seed(db, 1, report(85))
    seed(db, 2, report(20, verdict=Verdict.NO_FIT))
    item = next(c for c in api.get("/api/candidates").json() if c["id"] == cid)
    assert item["position"] == "Инженер-программист"
    assert item["best_vacancy"] == "1" and item["best_title"] == "Вакансия 1"
    assert item["best_score"] == 85 and item["name"] is None and item["waiting"] is True
    named = next(c for c in api.get("/api/candidates?pii=true").json() if c["id"] == cid)
    assert named["name"] == "Иванов Иван"
    assert api.get(f"/api/candidates/{cid}?pii=true").json()["name"] == "Иванов Иван"
    r = api.post(f"/api/candidates/{cid}/reply-sent", json={"template": "invite"})
    assert r.status_code == 200, r.text
    card = api.get(f"/api/candidates/{cid}").json()
    assert card["waiting"] is False and card["events"][-1]["type"] == "reply_sent"
    stats = api.get("/api/stats").json()
    assert stats["total"] == 2 and stats["waiting"] == 1 and stats["by_status"]["new"] == 2
    assert stats["avg_reply_days"] is not None
    assert api.post("/api/candidates/999/reply-sent", json={}).status_code == 404


def test_package(api: TestClient, db: Path) -> None:
    a = seed(db, 1, report(85))
    b = seed(db, 2, report(60, verdict=Verdict.PARTIAL))
    body = {"vacancy_id": "1", "candidate_ids": [a, b], "names": False}
    r = api.post("/api/package", json=body)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    doc = Docx(io.BytesIO(r.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    table = doc.tables[0]
    cells = [[c.text for c in row.cells] for row in table.rows]
    assert cells[0] == ["Требование", f"Кандидат №{a}", f"Кандидат №{b}"]
    assert [row[0] for row in cells[1:3]] == ["Python", "SQL"]  # только обязательные
    assert cells[1][1] == "есть" and cells[2][1] == "нет"
    for part in ("Кратко", "Риски", "Вопросы на собеседование", "Расскажите про опыт с SQL."):
        assert part in text
    assert "дети" not in text and "Иванов" not in text  # юр. фильтр, ФИО скрыты
    for cid in (a, b):
        assert api.get(f"/api/candidates/{cid}").json()["status"] == "cust"
    params: dict[str, Any] = {"vacancy_id": "1", "candidate_ids": [a], "names": "true"}
    h = api.get("/api/package", params=params | {"format": "html"})
    assert h.status_code == 200 and h.headers["content-type"].startswith("text/html")
    assert "<table" in h.text and "Иванов Иван" in h.text and "дети" not in h.text
    assert api.post("/api/package", json=body | {"vacancy_id": "77"}).status_code == 404


HH_LIST = {
    "items": [
        {
            "id": "101",
            "name": "Инженер-исследователь",
            "alternate_url": "https://hh.ru/vacancy/101",
            "area": {"name": "Долгопрудный"},
            "published_at": "2026-11-01T10:00:00+0300",
        }
    ],
    "pages": 1,
}
HH_ONE = {
    "id": "101",
    "name": "Инженер-исследователь",
    "description": "<p>Требования:</p><ul><li>Python &amp; SQL</li><li>Linux</li></ul>",
    "key_skills": [{"name": "Git"}],
}


def test_hh(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fetch(url: str, params: dict[str, Any] | None = None) -> Any:
        calls.append(url)
        return HH_ONE if url.endswith("/vacancies/101") else HH_LIST

    monkeypatch.setattr(hh, "fetch_json", fetch)
    items = api.get("/api/vacancies/hh").json()
    assert items[0]["id"] == "101" and items[0]["imported"] is False
    api.get("/api/vacancies/hh")
    assert len(calls) == 1  # кэш на сутки
    r = api.post("/api/vacancies/hh/import", json={"ids": ["101"]})
    assert r.status_code == 200, r.text
    vac = next(v for v in api.get("/api/vacancies").json() if v["source"] == "hh:101")
    assert vac["title"] == "Инженер-исследователь"
    assert api.get("/api/vacancies/hh").json()[0]["imported"] is True
    again = api.post("/api/vacancies/hh/import", json={"ids": ["101"]}).json()
    assert again["skipped"] == ["101"] and again["added"] == []
    assert api.post("/api/vacancies/hh/import", json={"ids": ["../x"]}).status_code == 422


def test_hh_forbidden(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(url: str, params: dict[str, Any] | None = None) -> Any:
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))

    monkeypatch.setattr(hh, "fetch_json", fetch)
    r = api.get("/api/vacancies/hh")
    assert r.status_code == 502 and "загрузка с hh не настроена на сервере" in r.json()["detail"]
