"""«Сводка»: цифры /api/dashboard на пустой базе и на журнале с известными датами."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import test_api
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_api import client
from test_flow import report

from rvm import store
from rvm.models import Document, Line, Status
from rvm.pipeline.analyze import ReportStatus
from rvm.privacy import Masked
from rvm.store import db as db_mod
from rvm.store.metrics import dashboard, period_start

# фикстуры с демо-сессией из test_api
_salt, fake, app, api = test_api._salt, test_api.fake, test_api.app, test_api.api

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    now = [NOW]
    monkeypatch.setattr(db_mod, "_now", lambda: now[0].isoformat())
    return now


def add(db: Path, i: int, source: str) -> tuple[int, int]:
    doc = Document(name=f"r{i}.txt", lines=[Line(n=1, text=f"Резюме {i}")])
    masked = Masked(doc=doc, replacements=[], contact_hashes=[], found={})
    added = store.add_resume(db, f"резюме {i}".encode(), doc, masked, source=source)
    return added.candidate_id, added.resume_id


def test_empty_base(tmp_path: Path) -> None:
    db = tmp_path / "rvm.sqlite"
    store.init(db)
    d = dashboard(db, 7, tmp_path / "нет.jsonl", NOW)
    assert d.since.isoformat() == "2026-11-08" and d.today.isoformat() == "2026-11-14"
    assert d.applications == 0 and d.by_source == {} and d.vacancies == []
    assert [x.total for x in d.flow] == [0] * 7 and d.flow[-1].by_source == {}
    assert d.queue.model_dump() == {"count": 0, "oldest_days": None, "over_sla": 0, "sla_days": 2}
    assert d.customer.model_dump() == {"count": 0, "oldest_days": None}
    assert d.kept.model_dump() == {"alt": 0, "reserve": 0, "total": 0}
    assert d.quality.model_dump() == {"spot_checked": 0, "spot_agreed": 0, "rows_fixed": 0}
    assert d.model.model_dump() == {
        "resumes": 0,
        "avg_seconds": None,
        "errors": 0,
        "unfinished": 0,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": None,
    }


@pytest.fixture
def base(tmp_path: Path, clock: list[datetime]) -> Path:
    """A — сегодня, к заказчику; B — 10.11 почтой, ждёт разбора; C — 12.11, отклик на 2,
    заказчик решил через сутки; D — 01.11, в резерв 13.11; E — 13.11, отказ; F — сегодня с hh,
    обезличивание не прошло; G — сегодня из папки, разбора нет."""
    db = tmp_path / "rvm.sqlite"
    store.init(db)
    for n in (1, 2):
        store.add_vacancy(db, Document(name="v", lines=[Line(n=1, text=f"Вакансия {n}")]))

    def cand(i: int, when: datetime, source: str, elapsed: float | None, **kw: Any) -> int:
        clock[0] = when
        cid, rid = add(db, i, source)
        if elapsed is not None:
            store.save_report(db, rid, report(**kw).model_copy(update={"elapsed_s": elapsed}))
        return cid

    def move(cid: int, status: store.CandidateStatus, when: datetime) -> None:
        clock[0] = when
        store.set_status(db, cid, status)

    S = store.CandidateStatus
    a = cand(1, at(14, 10), "файл", 10)
    b = cand(2, at(10, 9), "почта", 20)
    c = cand(3, at(12, 9), "файл", 30, redirect=True)  # отклик на вакансию 2
    d = cand(4, at(1, 9), "файл", 40)
    e = cand(5, at(13, 8), "файл", 50)
    clock[0] = at(14, 10)
    f, rid_f = add(db, 6, "hh")
    privacy = report().model_copy(
        update={"status": ReportStatus.PRIVACY_ERROR, "vacancies": [], "applied": None}
    )
    store.save_report(db, rid_f, privacy)
    add(db, 7, "папка")

    move(a, S.CUST, at(14, 11))
    move(c, S.CUST, at(12, 12))
    move(c, S.INVITED, at(13, 12))
    move(d, S.RESERVE, at(13, 9))
    move(e, S.REJECTED, at(13, 10))
    clock[0] = at(13, 15)
    store.log(db, "spot_check", d, agree=False)
    clock[0] = at(14, 11)
    store.log(db, "spot_check", b, agree=True)
    rid_a = store.card(db, a)["versions"][0]["id"]
    for req in ("R2", "R2", "R1"):  # повторный ответ по той же строке — одна строка
        store.answer(db, rid_a, "1", req, Status.FULL)

    lines = [
        {"at": at(14, 9).isoformat(), "prompt_tokens": 10, "completion_tokens": 5},
        {"at": at(10, 9).isoformat(), "prompt_tokens": 100, "completion_tokens": 50},
        {"at": at(1, 9).isoformat(), "prompt_tokens": 1000, "completion_tokens": 500},
        {"prompt_tokens": 7, "completion_tokens": 7},  # запись до метки времени
    ]
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        "\n".join(map(json.dumps, lines)) + '\n[1, 2]\n"строка"\nnull\n{"at": "2026-11-14', "utf-8"
    )
    assert f
    return db


def funnel(d: Any) -> dict[str | None, dict[str, Any]]:
    return {v.vacancy_id: v.model_dump(exclude={"vacancy_id"}) for v in d.vacancies}


def test_week(base: Path) -> None:
    d = dashboard(base, 7, base.parent / "usage.jsonl", NOW)
    assert d.applications == 6
    assert d.by_source == {"файл": 3, "почта": 1, "hh": 1, "папка": 1}
    assert {x.date.day: (x.total, x.by_source) for x in d.flow if x.total} == {
        10: (1, {"почта": 1}),
        12: (1, {"файл": 1}),
        13: (1, {"файл": 1}),
        14: (3, {"файл": 1, "hh": 1, "папка": 1}),
    }
    assert len(d.flow) == 7 and d.flow[0].date.day == 8
    assert d.queue.model_dump() == {"count": 3, "oldest_days": 4, "over_sla": 1, "sla_days": 2}
    assert d.customer.model_dump() == {"count": 1, "oldest_days": 0}
    assert d.kept.model_dump() == {"alt": 0, "reserve": 1, "total": 1}
    assert d.quality.model_dump() == {"spot_checked": 2, "spot_agreed": 1, "rows_fixed": 2}
    assert [v.vacancy_id for v in d.vacancies] == ["1", None, "2"]  # по числу откликов
    assert funnel(d) == {
        "1": {
            "title": "Вакансия 1",
            "applied": 3,  # A, B, E
            "reviewed": 2,
            "to_customer": 1,
            "customer_decided": 0,
            "rejected": 1,
            "kept": 0,
            "median_decision_days": 0.1,  # A 1 ч, E 2 ч → 1,5 ч
            "median_customer_days": None,
            "customer_waiting": 1,
            "customer_oldest_days": 0,
        },
        None: {
            "title": None,
            "applied": 2,  # F и G: вакансия отклика не определена
            "reviewed": 0,
            "to_customer": 0,
            "customer_decided": 0,
            "rejected": 0,
            "kept": 0,
            "median_decision_days": None,
            "median_customer_days": None,
            "customer_waiting": 0,
            "customer_oldest_days": None,
        },
        "2": {
            "title": "Вакансия 2",
            "applied": 1,
            "reviewed": 1,
            "to_customer": 1,
            "customer_decided": 1,
            "rejected": 0,
            "kept": 0,
            "median_decision_days": 0.1,  # 3 ч
            "median_customer_days": 1.0,
            "customer_waiting": 0,
            "customer_oldest_days": None,
        },
    }
    assert d.model.model_dump() == {
        "resumes": 4,  # A, B, C, E; D загружен раньше периода
        "avg_seconds": 27.5,
        "errors": 1,
        "unfinished": 1,
        "calls": 2,
        "prompt_tokens": 110,
        "completion_tokens": 55,
        "cost_usd": None,
    }


def test_days_filter(base: Path) -> None:
    usage = base.parent / "usage.jsonl"
    today = dashboard(base, 1, usage, NOW)
    assert today.since == today.today and len(today.flow) == 1
    assert today.applications == 3 and today.by_source == {"файл": 1, "hh": 1, "папка": 1}
    assert today.queue.count == 3  # очередь — срез на сейчас, не за период
    assert today.kept.total == 0
    assert today.quality.model_dump() == {"spot_checked": 1, "spot_agreed": 1, "rows_fixed": 2}
    assert {v.vacancy_id: v.applied for v in today.vacancies} == {None: 2, "1": 1}
    assert today.model.resumes == 1 and today.model.avg_seconds == 10
    assert today.model.calls == 1 and today.model.prompt_tokens == 10
    month = dashboard(base, 30, usage, NOW)
    assert month.applications == 7 and len(month.flow) == 30
    assert month.model.resumes == 5 and month.model.calls == 3


def test_sla_from_config(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rvm.scoring.config.SLA_DAYS", 5)
    assert dashboard(base, 7, None, NOW).queue.over_sla == 0


def test_api_dashboard(app: FastAPI, api: TestClient) -> None:
    r = api.get("/api/dashboard", params={"days": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 30 and body["applications"] == 0 and len(body["flow"]) == 30
    assert body["model"]["cost_usd"] is None
    assert api.get("/api/dashboard", params={"days": 3}).status_code == 422
    with client(app, "manager") as m:
        assert m.get("/api/dashboard").status_code == 403


def test_undo_not_counted(tmp_path: Path, clock: list[datetime]) -> None:
    """«Отменить» в «Разборе» и на карточке: ни отмена, ни отменённое решение не считаются."""
    db = tmp_path / "rvm.sqlite"
    store.init(db)
    S = store.CandidateStatus
    clock[0] = at(13, 9)
    a, _ = add(db, 1, "файл")
    b, _ = add(db, 2, "файл")
    clock[0] = at(13, 10)
    store.set_status(db, a, S.CUST)
    store.set_status(db, b, S.RESERVE)
    clock[0] = at(13, 11)
    store.set_status(db, a, S.NEW, undo=True)
    store.set_status(db, b, S.NEW, undo=True)
    d = dashboard(db, 7, None, NOW)
    (v,) = d.vacancies
    assert (v.reviewed, v.to_customer, v.customer_decided) == (0, 0, 0)
    assert v.median_decision_days is None and v.median_customer_days is None
    assert d.kept.total == 0 and d.queue.count == 2
    clock[0] = at(13, 12)
    store.set_status(db, a, S.REJECTED)  # решение после отмены — считается
    v = dashboard(db, 7, None, NOW).vacancies[0]
    assert v.reviewed == 1 and v.median_decision_days == 0.1


def test_today_in_rvm_tz(
    tmp_path: Path, clock: list[datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "rvm.sqlite"
    store.init(db)
    clock[0] = at(14, 20)  # 23:00 МСК 14.11
    add(db, 1, "файл")
    clock[0] = at(14, 21)  # 00:00 МСК 15.11
    add(db, 2, "почта")
    late = datetime(2026, 11, 14, 21, 30, tzinfo=UTC)  # по UTC ещё 14.11
    d = dashboard(db, 1, None, late)
    assert d.today.isoformat() == "2026-11-15" and d.by_source == {"почта": 1}
    assert period_start(1, late) == "2026-11-14T21:00:00+00:00"  # полночь МСК в UTC
    # список кандидатов за 1 день — то же число, что «Сводка» за сегодня
    assert store.count_candidates(db, since=period_start(1, late)) == d.applications == 1
    assert store.count_candidates(db, since=period_start(2, late)) == 2
    monkeypatch.setattr("rvm.scoring.config.TZ", ZoneInfo("UTC"))
    d = dashboard(db, 1, None, late)
    assert d.today.isoformat() == "2026-11-14" and d.by_source == {"файл": 1, "почта": 1}


def test_env_empty_or_bad_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from rvm.scoring.config import _env

    monkeypatch.setenv("RVM_SLA_DAYS", "")
    monkeypatch.setenv("RVM_TZ", " ")
    assert _env("RVM_SLA_DAYS", "2", int) == 2
    assert _env("RVM_TZ", "Europe/Moscow", ZoneInfo) == ZoneInfo("Europe/Moscow")
    assert not caplog.records  # пустое — молча по умолчанию
    monkeypatch.setenv("RVM_SLA_DAYS", "два")
    monkeypatch.setenv("RVM_TZ", "Марс/Олимп")
    assert _env("RVM_SLA_DAYS", "2", int) == 2
    assert _env("RVM_TZ", "Europe/Moscow", ZoneInfo) == ZoneInfo("Europe/Moscow")
    assert [r.levelname for r in caplog.records] == ["WARNING", "WARNING"]
    assert "RVM_TZ" in caplog.records[1].getMessage()
    monkeypatch.setenv("RVM_SLA_DAYS", "5")
    monkeypatch.setenv("RVM_TZ", "UTC")
    assert _env("RVM_SLA_DAYS", "2", int) == 5
    assert _env("RVM_TZ", "Europe/Moscow", ZoneInfo) == ZoneInfo("UTC")
