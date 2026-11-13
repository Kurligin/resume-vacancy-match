import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rvm import store
from rvm.models import Document, Line, MatchRow, ReqKind, Requirement, Section, Status
from rvm.pipeline.analyze import Report, ReportStatus, ResumeView, VacancyResult
from rvm.pipeline.explain import Insights, Point
from rvm.privacy import Masked, mask
from rvm.scoring import rank_vacancies, score_pair

DATA_DIR = os.environ.get("RVM_DATA_DIR")
PHONE = "+7 (999) 123-45-67"


@pytest.fixture(autouse=True)
def _salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "var" / "rvm.sqlite"
    store.init(path)
    return path


def doc(*texts: str, name: str = "Петров_Иван.docx") -> Document:
    return Document(name=name, lines=[Line(n=i, text=t) for i, t in enumerate(texts, 1)])


def masked(d: Document, hashes: list[str]) -> Masked:
    return Masked(doc=d, replacements=[], contact_hashes=hashes, found={})


def report(name: str, statuses: dict[str, dict[str, Status]], applied: str = "v1") -> Report:
    """Отчёт без модели: по вакансии — требования must r1, r2 со статусами."""
    results = {}
    for vid, st in statuses.items():
        reqs = [
            Requirement(id=r, text=f"навык {r}", section=Section.MUST, kind=ReqKind.SKILL)
            for r in st
        ]
        rows = [MatchRow(requirement_id=r, status=s) for r, s in st.items()]
        sc = score_pair(reqs, rows)
        results[vid] = VacancyResult(
            id=vid,
            title=f"Вакансия {vid}",
            requirements=reqs,
            rows=rows,
            score=sc,
            verdict=sc.verdict,
            place=None,
        )
    ranking = rank_vacancies({v: r.score for v, r in results.items()}, applied)
    for p in ranking.places:
        results[p.id].place = p.place
    return Report(
        status=ReportStatus.OK,
        resume=ResumeView(name=name, lines=[], original=[], status="ok"),
        applied=applied,
        applied_guessed=False,
        vacancies=[results[p.id] for p in ranking.places],
        redirect=ranking.redirect,
        none_fit=ranking.none_fit,
        facts=None,
        warnings=[f"{name}: таблица прочитана частично"],
        model="fake",
        prompt_versions={"match": "1"},
        created_at=datetime.now(UTC),
        elapsed_s=0.1,
    )


def test_same_file_twice_one_version(db: Path) -> None:
    d = doc("Python-разработчик", f"Телефон {PHONE}")
    a = store.add_resume(db, b"file-1", d, mask(d))
    b = store.add_resume(db, b"file-1", d, mask(d))
    assert a.new and not b.new
    assert (a.candidate_id, a.resume_id) == (b.candidate_id, b.resume_id)
    assert len(store.card(db, a.candidate_id)["versions"]) == 1


def test_other_file_same_phone_new_card_flagged(db: Path) -> None:
    d1 = doc("Python-разработчик", f"Телефон {PHONE}")
    d2 = doc("Senior Python", "Тел.: 8 999 123 45 67", "ivan@example.com")
    a = store.add_resume(db, b"v1", d1, mask(d1))
    b = store.add_resume(db, b"v2", d2, mask(d2))
    assert a.candidate_id != b.candidate_id and b.new
    second = store.card(db, b.candidate_id)
    assert second["possible_duplicate_of"] == a.candidate_id
    ev = [e for e in second["events"] if e["type"] == "possible_duplicate"]
    assert ev[0]["details"] == {"of": a.candidate_id} and ev[0]["who"] == "система"
    assert store.card(db, a.candidate_id)["possible_duplicate_of"] is None
    other = doc("Аналитик", "Телефон +7 912 000-00-00")
    c = store.add_resume(db, b"v3", other, mask(other))
    assert store.card(db, c.candidate_id)["possible_duplicate_of"] is None


def test_merge_candidates(db: Path) -> None:
    d1 = doc("Python-разработчик", f"Телефон {PHONE}")
    d2 = doc("Senior Python", f"Тел. {PHONE}")
    a = store.add_resume(db, b"v1", d1, mask(d1))
    b = store.add_resume(db, b"v2", d2, mask(d2))
    store.save_report(db, b.resume_id, report(d2.name, {"v1": {"r1": Status.FULL}}))
    store.merge_candidates(db, a.candidate_id, b.candidate_id)

    assert [c["id"] for c in store.list_candidates(db)] == [a.candidate_id]
    card = store.card(db, a.candidate_id)
    assert [v["id"] for v in card["versions"]] == [b.resume_id, a.resume_id]
    assert card["evaluations"][0]["resume_id"] == b.resume_id
    types = [e["type"] for e in card["events"]]
    assert {"uploaded", "evaluated", "possible_duplicate", "merged"} <= set(types)
    merged = next(e for e in card["events"] if e["type"] == "merged")
    assert merged["who"] == "рекрутер" and merged["details"] == {"from": b.candidate_id}
    with pytest.raises(KeyError):
        store.card(db, b.candidate_id)
    # тот же телефон после склейки — снова пометка на ту же карточку
    d3 = doc("Python", f"Телефон {PHONE}")
    c = store.add_resume(db, b"v3", d3, mask(d3))
    assert store.card(db, c.candidate_id)["possible_duplicate_of"] == a.candidate_id
    with pytest.raises(ValueError):
        store.merge_candidates(db, a.candidate_id, a.candidate_id)
    with pytest.raises(KeyError):
        store.merge_candidates(db, a.candidate_id, 999)


def test_dismiss_duplicate(db: Path) -> None:
    d1 = doc("Python", f"Телефон {PHONE}")
    d2 = doc("Аналитик", f"Телефон {PHONE}")
    a = store.add_resume(db, b"v1", d1, mask(d1))
    b = store.add_resume(db, b"v2", d2, mask(d2))
    store.dismiss_duplicate(db, b.candidate_id)
    card = store.card(db, b.candidate_id)
    assert card["possible_duplicate_of"] is None
    ev = [e for e in card["events"] if e["type"] == "not_duplicate"]
    assert ev[0]["details"] == {"of": a.candidate_id}
    with pytest.raises(ValueError):
        store.dismiss_duplicate(db, b.candidate_id)


def test_deleted_id_not_reused(db: Path) -> None:
    ids = [
        store.add_resume(db, f"f{i}".encode(), doc(f"r{i}"), masked(doc("x"), [])) for i in range(5)
    ]
    last = ids[-1]
    store.delete_candidate(db, last.candidate_id)
    new = store.add_resume(db, b"f-new", doc("new"), masked(doc("new"), []))
    assert new.candidate_id > last.candidate_id and new.resume_id > last.resume_id


def test_migration_from_v1_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(store.db._MIGRATIONS[0] + "PRAGMA user_version=1;")
        conn.execute("INSERT INTO candidates (id, created_at, source) VALUES (3, 'x', 'файл')")
        conn.execute(
            "INSERT INTO resumes (id, candidate_id, sha256, name, status, lines, original,"
            " created_at) VALUES (4, 3, 's', 'n', 'ok', '[]', '[]', 'x')"
        )
        conn.execute(
            "INSERT INTO events (at, type, candidate_id, who, details)"
            " VALUES ('x', 'deleted', 7, 'рекрутер', '{}')"
        )
    store.init(path)
    store.init(path)
    assert store.card(path, 3)["versions"][0]["id"] == 4
    a = store.add_resume(path, b"new", doc("new"), masked(doc("new"), []))
    assert a.candidate_id == 8 and a.resume_id == 5  # 4–7 заняты, в том числе удалённой 7


def test_cleanup_busy_raises(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.db, "_CLEANUP_S", 0.3)
    a = store.add_resume(db, b"a", doc("a"), masked(doc("a"), ["h"]))
    reader = sqlite3.connect(db, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM candidates").fetchone()
        with pytest.raises(store.CleanupError):
            store.delete_candidate(db, a.candidate_id)
        reader.execute("COMMIT")
    finally:
        reader.close()
    store.cleanup(db)  # читатель ушёл — очистка проходит
    with pytest.raises(KeyError):
        store.card(db, a.candidate_id)


def test_status_change_writes_event(db: Path) -> None:
    cid = store.add_resume(db, b"x", doc("a"), masked(doc("a"), ["h"])).candidate_id
    store.set_status(db, cid, store.CandidateStatus.RESERVE)
    card = store.card(db, cid)
    assert card["status"] == "reserve"
    ev = [e for e in card["events"] if e["type"] == "status"]
    assert ev[0]["who"] == "рекрутер" and ev[0]["details"] == {"from": "new", "to": "reserve"}
    with pytest.raises(ValueError):
        store.set_status(db, cid, "hired")  # type: ignore[arg-type]


def test_vacancies_add_close_list(db: Path) -> None:
    v1 = store.add_vacancy(db, doc("Python-разработчик", name="python.docx"))
    v2 = store.add_vacancy(db, doc("Аналитик", name="analyst.docx"), source="текст")
    store.close_vacancy(db, v1)
    assert [v["id"] for v in store.list_vacancies(db)] == [v1, v2]
    assert list(store.open_vacancies(db)) == [v2]
    assert store.open_vacancies(db)[v2].lines[0].text == "Аналитик"


def test_save_report_list_and_card(db: Path) -> None:
    d = doc("Python-разработчик, Django", name="Петров_Иван.docx")
    a = store.add_resume(db, b"a", d, masked(d, ["ha"]))
    rep = report(d.name, {"v1": {"r1": Status.FULL, "r2": Status.UNKNOWN}})
    store.save_report(db, a.resume_id, rep)
    e = doc("Аналитик данных", name="b.docx")
    b = store.add_resume(db, b"b", e, masked(e, ["hb"]))
    store.save_report(db, b.resume_id, report("b.docx", {"v1": {"r1": Status.FULL}}))

    by_score = store.list_candidates(db)
    assert [c["id"] for c in by_score] == [b.candidate_id, a.candidate_id]
    assert next(c["id"] for c in store.list_candidates(db, sort="date")) == b.candidate_id
    assert [c["id"] for c in store.list_candidates(db, q="DJANGO")] == [a.candidate_id]
    assert [c["id"] for c in store.list_candidates(db, q="аналитик")] == [b.candidate_id]
    assert store.list_candidates(db, status=store.CandidateStatus.REJECTED) == []

    card = store.card(db, a.candidate_id)
    assert card["evaluations"][0]["vacancy_id"] == "v1"
    assert {e["type"] for e in card["events"]} == {"uploaded", "evaluated"}
    back = store.load_report(db, a.resume_id)
    assert back.vacancies[0].score == rep.vacancies[0].score
    assert back.resume.original[0].text == d.lines[0].text
    # имя файла с ФИО не попадает в оценки и журнал
    with sqlite3.connect(db) as conn:
        blob = str(conn.execute("SELECT * FROM evaluations").fetchall())
        blob += str(conn.execute("SELECT * FROM events").fetchall())
        blob += str(conn.execute("SELECT analysis FROM resumes").fetchall())
    assert "Петров" not in blob


def test_answer_unknown_to_full_recomputes(db: Path) -> None:
    d = doc("Python")
    a = store.add_resume(db, b"a", d, masked(d, ["h"]))
    rep = report(d.name, {"v1": {"r1": Status.FULL, "r2": Status.UNKNOWN}})
    store.save_report(db, a.resume_id, rep)
    before = rep.vacancies[0].score.score
    after = store.answer(db, a.resume_id, "v1", "r2", Status.FULL, note="подтвердил на звонке")
    assert after.score > before
    res = store.load_report(db, a.resume_id).vacancies[0]
    row = next(r for r in res.rows if r.requirement_id == "r2")
    assert row.status == Status.FULL and store.FROM_CANDIDATE in row.flags
    assert res.score == after
    ev = [e for e in store.card(db, a.candidate_id)["events"] if e["type"] == "answer"]
    assert ev[0]["details"]["requirement"] == "r2" and ev[0]["details"]["after"] == after.score
    assert ev[0]["vacancy_id"] == "v1"


def test_answer_rebuilds_insights(db: Path) -> None:
    d = doc("Python")
    a = store.add_resume(db, b"a", d, masked(d, ["h"]))
    rep = report(d.name, {"v1": {"r1": Status.FULL, "r2": Status.NO}})
    old = rep.vacancies[0]
    old.insights = Insights(
        strengths=[],
        risks=[Point(text="Не выполнено: навык r2", refs=["r2"])],
        recommendation=Point(text=f"Отказать: балл {old.score.score}.", refs=["VERDICT"]),
    )
    store.save_report(db, a.resume_id, rep)
    after = store.answer(db, a.resume_id, "v1", "r2", Status.FULL)
    ins = store.load_report(db, a.resume_id).vacancies[0].insights
    assert ins is not None and ins.fallback
    assert ins.risks == [] and {"r1", "r2"} == {x for p in ins.strengths for x in p.refs}
    assert ins.recommendation.text == f"Пригласить на собеседование: балл {after.score}."


def test_answer_on_check_keeps_score(db: Path) -> None:
    d = doc("Python")
    a = store.add_resume(db, b"a", d, masked(d, ["h"]))
    rep = report(d.name, {"v1": {"r1": Status.FULL, "r2": Status.UNKNOWN}, "v2": {"r1": Status.NO}})
    risk = Point(text="Резюме составлено давно", refs=["SNAPSHOT"])
    for v in rep.vacancies:
        v.insights = Insights(
            strengths=[],
            risks=[risk, Point(text="Нет данных по r2", refs=["r2"])],
            recommendation=Point(text="звать", refs=["VERDICT"]),
        )
    store.save_report(db, a.resume_id, rep)
    before = {v.id: v.score for v in store.load_report(db, a.resume_id).vacancies}

    after = store.answer(db, a.resume_id, "v1", "SNAPSHOT", Status.FULL, note="резюме актуально")
    assert after == before["v1"]
    for v in store.load_report(db, a.resume_id).vacancies:
        assert v.score == before[v.id]
        assert v.clarified == {"SNAPSHOT": f"{store.CLARIFIED}: резюме актуально"}
        assert v.insights is not None and [r.refs for r in v.insights.risks] == [["r2"]]
    ev = [e for e in store.card(db, a.candidate_id)["events"] if e["type"] == "clarified"]
    assert ev[0]["details"] == {"check": "SNAPSHOT"}
    assert ev[0]["vacancy_id"] == "v1"
    with pytest.raises(KeyError):
        store.answer(db, a.resume_id, "v1", "D7", Status.FULL)


def test_delete_erases_everything(db: Path) -> None:
    d = doc("Уникальная строка Зюзюкин", f"Телефон {PHONE}", name="Зюзюкин.docx")
    m = mask(d)
    a = store.add_resume(db, b"a", d, m)
    store.save_report(db, a.resume_id, report(d.name, {"v1": {"r1": Status.FULL}}))
    store.set_status(db, a.candidate_id, store.CandidateStatus.REJECTED)
    keep = store.add_resume(db, b"k", doc("другой"), masked(doc("другой"), ["hk"]))
    store.delete_candidate(db, a.candidate_id)

    with sqlite3.connect(db) as conn:
        dump = "\n".join(conn.iterdump())
        events = conn.execute(
            "SELECT type, details FROM events WHERE candidate_id=?", (a.candidate_id,)
        ).fetchall()
        counts = [
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("candidates", "resumes", "contacts", "evaluations")
        ]
    assert counts == [1, 1, 1, 0]  # остался только второй кандидат
    assert "Зюзюкин" not in dump and m.contact_hashes[0] not in dump
    assert events == [("deleted", "{}")]
    wal = db.with_name(db.name + "-wal")
    raw = db.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert "Зюзюкин".encode() not in raw and m.contact_hashes[0].encode() not in raw
    assert store.card(db, keep.candidate_id)["id"] == keep.candidate_id
    with pytest.raises(KeyError):
        store.card(db, a.candidate_id)


def test_hundred_candidates_from_eight_threads(db: Path) -> None:
    def add(i: int) -> int:
        d = doc(f"резюме {i}")
        hashes = [f"h{i}"] if i % 10 else []  # без контактов — своя карточка
        return store.add_resume(db, f"file-{i}".encode(), d, masked(d, hashes)).candidate_id

    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(add, range(100)))
    assert len(set(ids)) == 100
    assert len(store.list_candidates(db)) == 100


@pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")
def test_case_file_twice(db: Path) -> None:
    from rvm.ingest import load_path

    path = min(Path(DATA_DIR or ".").rglob("*.docx"))
    d = load_path(path)
    a = store.add_resume(db, path.read_bytes(), d, mask(d, path.name))
    b = store.add_resume(db, path.read_bytes(), d, mask(d, path.name))
    assert a.resume_id == b.resume_id and len(store.card(db, a.candidate_id)["versions"]) == 1


def test_recruiter_edit_flag_replaces_candidate_flag(db: Path) -> None:
    d = doc("Python")
    a = store.add_resume(db, b"a", d, masked(d, ["h"]))
    store.save_report(db, a.resume_id, report(d.name, {"v1": {"r1": Status.UNKNOWN}}))
    store.answer(db, a.resume_id, "v1", "r1", Status.FULL)
    store.answer(db, a.resume_id, "v1", "r1", Status.PARTIAL, flag=store.BY_RECRUITER, who="Анна")
    row = store.load_report(db, a.resume_id).vacancies[0].rows[0]
    assert row.status == Status.PARTIAL and row.flags == [store.BY_RECRUITER]
    ev = store.card(db, a.candidate_id)["events"][-1]
    assert ev["type"] == "edited" and ev["who"] == "Анна"


def test_set_status_with_comment(db: Path) -> None:
    d = doc("Python")
    a = store.add_resume(db, b"a", d, masked(d, ["h"]))
    store.set_status(
        db, a.candidate_id, store.CandidateStatus.CUST_REJECTED, who="Олег", comment="нет"
    )
    ev = store.card(db, a.candidate_id)["events"][-1]
    assert ev["details"] == {"from": "new", "to": "cust_rejected", "comment": "нет"}
