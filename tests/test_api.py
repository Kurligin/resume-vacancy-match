import io
import os
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_analyze import RoutingFake

import rvm
from rvm.api import auth, create_app
from rvm.llm import LLMClient
from rvm.store.metrics import period_start

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")
EXTERNAL = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")


class SwitchFake(RoutingFake):
    """full=True — сопоставление отвечает «выполнено» по всем требованиям."""

    full = False
    hook: Any = None  # вызывается один раз при первом ответе с full=True

    def _create(self, **kwargs: Any) -> Any:
        reply = super()._create(**kwargs)
        if self.full and self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.full:
            msg = reply.choices[0].message
            msg.content = msg.content.replace(
                '"status": "unknown", "evidence": []',
                '"status": "full", "evidence": [{"line": 3, "quote": "Python"}]',
            )
        return reply


@pytest.fixture
def fake() -> SwitchFake:
    return SwitchFake()


HEADERS = {"X-Requested-With": "test"}


@pytest.fixture
def app(tmp_path: Path, fake: SwitchFake, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("RVM_SECRET", "тестовый-секрет")
    monkeypatch.setenv("RVM_DEMO_LOGIN", "1")
    llm = LLMClient(
        fake, base_url=EXTERNAL, model="fake", mode="live", cache_path=tmp_path / "c.db"
    )
    return create_app(db=tmp_path / "rvm.sqlite", llm=llm, today=date(2026, 11, 12))


def client(app: FastAPI, role: str | None = None) -> TestClient:
    c = TestClient(app, base_url="http://localhost", headers=HEADERS)
    if role:
        assert c.post("/api/login", json={"role": role}).status_code == 200
    return c


@pytest.fixture
def api(app: FastAPI) -> Any:
    with client(app, "recruiter") as c:
        yield c


def resume(i: int) -> bytes:
    return (f"Кандидат номер {i}\nТелефон: +7 916 000-00-{i:02d}\nОпыт: Python {i} лет\n").encode()


def wait(api: TestClient, url: str, done: Any) -> Any:
    for _ in range(400):
        body = api.get(url).json()
        if done(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"не дождались {url}: {body}")


def wait_batch(api: TestClient, batch_id: str) -> list[dict[str, Any]]:
    body = wait(
        api,
        f"/api/uploads/{batch_id}",
        lambda b: all(f["state"] in ("done", "error") for f in b["files"]),
    )
    return body["files"]


def add_vacancy(api: TestClient, text: str) -> str:
    r = api.post("/api/vacancies", data={"text": text})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(api: TestClient, files: list[tuple[str, bytes]], **params: str) -> str:
    r = api.post("/api/uploads", files=[("files", (n, d)) for n, d in files], params=params or None)
    assert r.status_code == 202, r.text
    return r.json()["batch_id"]


def test_zip_with_broken_file(api: TestClient) -> None:
    add_vacancy(api, "Python-разработчик\nТребования: Python")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(1, 5):
            z.writestr(f"резюме {i}.txt", resume(i))
        z.writestr("битое.docx", b"PK\x03\x04 not a docx")
        z.writestr("__MACOSX/._резюме 1.txt", b"x")
    files = wait_batch(api, upload(api, [("пачка.zip", buf.getvalue())]))
    assert sorted(f["state"] for f in files) == ["done"] * 4 + ["error"]
    bad = next(f for f in files if f["state"] == "error")
    assert bad["name"] == "битое.docx" and "повреждён" in bad["message"]
    assert len(api.get("/api/candidates").json()) == 4


def test_batch_limit_and_unknown_batch(api: TestClient) -> None:
    many = [(f"{i}.txt", resume(i % 100)) for i in range(301)]
    assert api.post("/api/uploads", files=[("files", f) for f in many]).status_code == 413
    assert api.get("/api/uploads/нет").status_code == 404


def test_answer_status_delete(api: TestClient) -> None:
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    (f,) = wait_batch(api, upload(api, [("r.txt", resume(7))], applied=vid))
    cid = f["candidate_id"]

    rating = api.get("/api/evaluations", params={"candidate": cid}).json()
    assert rating["applied"] == vid and [v["id"] for v in rating["vacancies"]] == [vid]
    rid = api.get(f"/api/candidates/{cid}").json()["versions"][0]["id"]
    pair = api.get(f"/api/evaluations/{rid}/{vid}").json()
    before = pair["result"]["score"]["score"]
    assert pair["resume"]["original"][0]["text"] == "Кандидат номер 7"
    req = pair["result"]["requirements"][0]["id"]

    r = api.patch(f"/api/evaluations/{rid}/{vid}/rows/{req}", json={"status": "full"})
    assert r.status_code == 200 and r.json()["score"] > before
    assert (
        api.patch(f"/api/evaluations/{rid}/{vid}/rows/R99", json={"status": "full"}).status_code
        == 404
    )

    assert api.patch(f"/api/candidates/{cid}", json={"status": "чепуха"}).status_code == 422
    assert api.patch(f"/api/candidates/{cid}", json={"status": "reserve"}).status_code == 200
    assert [c["id"] for c in api.get("/api/candidates", params={"status": "reserve"}).json()] == [
        cid
    ]
    assert api.patch(f"/api/candidates/{cid}", json={"status": "new", "undo": True}).is_success
    card = api.get(f"/api/candidates/{cid}").json()
    assert {e["type"] for e in card["events"]} >= {"uploaded", "evaluated", "answer", "status"}
    moves = [e["details"] for e in card["events"] if e["type"] == "status"]
    assert [m.get("undo") for m in moves] == [None, True]  # отмена помечена для «Сводки»

    assert api.delete(f"/api/candidates/{cid}").status_code == 204
    assert api.get(f"/api/candidates/{cid}").status_code == 404
    assert api.delete(f"/api/candidates/{cid}").status_code == 404


def test_new_vacancy_checks_reserve(api: TestClient, fake: SwitchFake) -> None:
    v1 = add_vacancy(api, "Python-разработчик\nТребования: Python")
    files = wait_batch(api, upload(api, [("a.txt", resume(1)), ("b.txt", resume(2))]))
    reserve = files[0]["candidate_id"]
    api.patch(f"/api/candidates/{reserve}", json={"status": "reserve"})
    rid = api.get(f"/api/candidates/{reserve}").json()["versions"][0]["id"]
    req = api.get(f"/api/evaluations/{rid}/{v1}").json()["result"]["requirements"][0]["id"]
    answered = api.patch(f"/api/evaluations/{rid}/{v1}/rows/{req}", json={"status": "full"})

    fake.full = True  # новая вакансия: резервист подходит
    v2 = add_vacancy(api, "Python-аналитик\nТребования: Python")
    vac = wait(
        api,
        "/api/vacancies",
        lambda b: next(v for v in b if v["id"] == v2)["reserve_state"] == "done",
    )
    new = next(v for v in vac if v["id"] == v2)
    assert new["reserve_checked"] == 1  # второй кандидат не в резерве — не трогаем
    card = api.get(f"/api/candidates/{reserve}").json()
    assert {e["vacancy_id"] for e in card["evaluations"]} == {v1, v2}
    assert new["reserve_matches"] == [reserve]
    old = api.get(f"/api/evaluations/{rid}/{v1}").json()["result"]["score"]
    assert old == answered.json()  # правка рекрутера по старой вакансии не потерялась

    assert api.post(f"/api/vacancies/{v1}/close").status_code == 200
    assert api.post("/api/vacancies/999/close").status_code == 404
    assert [v["is_open"] for v in api.get("/api/vacancies").json()] == [False, True]
    assert api.post("/api/vacancies", data={"text": "  "}).status_code == 422


def test_vacancy_candidates(app: FastAPI, api: TestClient, fake: SwitchFake) -> None:
    v1 = add_vacancy(api, "Python-разработчик\nТребования: Python")
    files = wait_batch(api, upload(api, [("a.txt", resume(1)), ("b.txt", resume(2))]))
    rows = api.get(f"/api/vacancies/{v1}/candidates").json()
    assert {r["id"] for r in rows} == {f["candidate_id"] for f in files}
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    r = rows[0]
    assert r["low"] <= r["score"] <= r["high"] and r["verdict"] and r["status"] == "new"
    assert r["resume_id"] == api.get(f"/api/candidates/{r['id']}").json()["versions"][0]["id"]
    assert all(isinstance(b, str) for b in r["blockers"])
    top = api.get(f"/api/vacancies/{v1}/candidates", params={"limit": 1}).json()
    assert top == rows[:1]
    v = next(x for x in api.get("/api/vacancies").json() if x["id"] == v1)
    assert v["rated"] == len(rows) and v["fit"] + v["partial"] <= v["rated"]

    fake.full = True  # вакансия, по которой у кандидатов ещё нет оценок, — пустой рейтинг
    v2 = add_vacancy(api, "Python-аналитик\nТребования: Python")
    assert api.get(f"/api/vacancies/{v2}/candidates").json() == []
    assert api.get("/api/vacancies/999/candidates").status_code == 404
    with client(app, "manager") as boss:
        assert boss.get(f"/api/vacancies/{v1}/candidates").status_code == 403


def test_health_docs_static(api: TestClient) -> None:
    h = api.get("/api/health").json()
    assert h["mode"] == "live" and h["model"] == "fake" and h["version"]
    assert api.get("/docs").status_code == 200
    assert "Загрузка резюме" in api.get("/openapi.json").text
    assert api.get("/").status_code == 200


def test_serve_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setattr("sys.argv", ["rvm", "serve", "--port", "9000"])
    rvm.main()
    assert seen["app"] == "rvm.api:create_app" and seen["factory"] and seen["port"] == 9000


@needs_data
def test_case_files_card(api: TestClient) -> None:
    files = sorted(Path(str(DATA_DIR)).glob("*.docx"))
    vacs = [f for f in files if "вакансия" in f.name.lower()]
    for f in vacs:
        r = api.post("/api/vacancies", files={"file": (f.name, f.read_bytes())})
        assert r.status_code == 201
    res = [f for f in files if f not in vacs]
    done = wait_batch(api, upload(api, [(f.name, f.read_bytes()) for f in res]))
    assert [f["state"] for f in done] == ["done"] * len(res)
    card = api.get(f"/api/candidates/{done[0]['candidate_id']}").json()
    assert len({e["vacancy_id"] for e in card["evaluations"]}) == 3


def test_ui_static(api: TestClient) -> None:
    page = api.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'src="/app.js"' in page.text and 'href="/app.css"' in page.text
    js, css = api.get("/app.js"), api.get("/app.css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert css.status_code == 200 and ".wrap" in css.text
    assert "http" not in js.text.replace("/api/", "")  # без CDN: работает без интернета
    assert 'src="/tour.js"' in page.text
    # «Сводка» — первый пункт шапки и только у рекрутера (data-rec прячет у руководителя)
    nav = page.text.index('href="#/dashboard" data-sec="dashboard" data-rec')
    assert nav < page.text.index('href="#/triage"')
    tour = api.get("/tour.js")
    assert tour.status_code == 200 and "http" not in tour.text


def test_ui_login_screen(app: FastAPI) -> None:
    """Страница и скрипт отдаются без сессии; экран входа в разметке; заголовок защиты в fetch."""
    with TestClient(app, base_url="http://localhost") as anon:
        page, js = anon.get("/"), anon.get("/app.js")
        assert page.status_code == 200 and js.status_code == 200
        assert 'id="login"' in page.text and 'id="logout"' in page.text
        assert '"X-Requested-With": "fetch"' in js.text
        assert anon.get("/api/me").status_code == 401


def test_password_hash() -> None:
    h = auth.hash_password("секрет")
    assert h.startswith("scrypt:") and "секрет" not in h
    assert auth.verify_password("секрет", h) and not auth.verify_password("другой", h)
    assert not auth.verify_password("секрет", "мусор")


def test_login_required(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_PASSWORD_ANNA", auth.hash_password("anna-demo"))
    with client(app) as anon:
        assert anon.get("/api/login-options").json() == {"demo": True}
        assert anon.post("/api/login", json={"login": "oleg", "password": ""}).status_code == 401
        assert anon.get("/api/health").status_code == 200
        assert anon.get("/api/candidates").status_code == 401
        assert anon.get("/api/me").status_code == 401
        bad = {"login": "anna", "password": "не тот"}
        assert anon.post("/api/login", json=bad).status_code == 401
        ok = {"login": "anna", "password": "anna-demo"}
        assert anon.post("/api/login", json=ok).status_code == 200
        assert anon.get("/api/me").json() == {
            "login": "anna",
            "name": "Анна Смирнова",
            "role": "recruiter",
        }
        assert anon.get("/api/candidates").status_code == 200
        forged = anon.cookies["rvm_session"].replace("anna.", "oleg.")
        anon.cookies.clear()
        anon.cookies.set("rvm_session", forged)
        assert anon.get("/api/candidates").status_code == 401  # подпись не сходится
        anon.post("/api/login", json={"role": "recruiter"})
        assert anon.post("/api/logout").status_code == 200
        assert anon.get("/api/candidates").status_code == 401


def test_csrf_header_and_host(app: FastAPI, api: TestClient) -> None:
    r = api.post("/api/vacancies", data={"text": "Python"}, headers={"X-Requested-With": ""})
    assert r.status_code == 403
    with TestClient(app, base_url="http://evil.example") as other:
        assert other.get("/api/health").status_code == 400


def test_manager_sees_only_cust_and_decides(app: FastAPI, api: TestClient) -> None:
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    a, b = (
        f["candidate_id"]
        for f in wait_batch(api, upload(api, [("a.txt", resume(1))]))
        + wait_batch(api, upload(api, [("b.txt", resume(2))]))
    )
    api.patch(f"/api/candidates/{a}", json={"status": "cust"})
    rid = api.get(f"/api/candidates/{a}").json()["versions"][0]["id"]
    rid_b = api.get(f"/api/candidates/{b}").json()["versions"][0]["id"]

    with client(app, "manager") as boss:
        assert [c["id"] for c in boss.get("/api/candidates").json()] == [a]
        assert boss.get(f"/api/candidates/{b}").status_code == 404
        assert boss.get(f"/api/evaluations/{rid}/{vid}").status_code == 200
        assert boss.get(f"/api/evaluations/{rid_b}/{vid}").status_code == 404
        assert boss.get("/api/evaluations", params={"candidate": b}).status_code == 404
        assert boss.patch(f"/api/candidates/{a}", json={"status": "new"}).status_code == 403
        assert boss.post("/api/vacancies", data={"text": "x"}).status_code == 403
        url = f"/api/candidates/{a}/manager-decision"
        r = boss.post(url, json={"decision": "invite", "comment": "сильный"})
        assert r.status_code == 200, r.text
        assert boss.post(url, json={"decision": "reject"}).status_code == 404  # уже не у заказчика

    card = api.get(f"/api/candidates/{a}").json()
    assert card["status"] == "invited"
    ev = card["events"][-1]
    assert ev["who"] == "Олег Волков (руководитель)" and ev["details"]["comment"] == "сильный"
    assert (
        api.post(f"/api/candidates/{b}/manager-decision", json={"decision": "invite"}).status_code
        == 403
    )


def test_recruiter_edit_and_who(api: TestClient) -> None:
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    (f,) = wait_batch(api, upload(api, [("r.txt", resume(3))]))
    rid = api.get(f"/api/candidates/{f['candidate_id']}").json()["versions"][0]["id"]
    req = api.get(f"/api/evaluations/{rid}/{vid}").json()["result"]["requirements"][0]["id"]
    url = f"/api/evaluations/{rid}/{vid}/rows/{req}"
    assert api.patch(url, json={"status": "full", "by": "recruiter"}).status_code == 200
    rows = api.get(f"/api/evaluations/{rid}/{vid}").json()["result"]["rows"]
    assert next(r for r in rows if r["requirement_id"] == req)["flags"] == ["изменено рекрутером"]
    assert (
        api.patch(f"/api/evaluations/{rid}/{vid}/rows/D7", json={"status": "full"}).status_code
        == 404
    )
    ev = api.get(f"/api/candidates/{f['candidate_id']}").json()["events"][-1]
    assert ev["type"] == "edited" and ev["who"] == "Анна Смирнова (рекрутер)"


def test_merge_and_dismiss(api: TestClient) -> None:
    add_vacancy(api, "Python-разработчик\nТребования: Python")
    (a,) = wait_batch(api, upload(api, [("a.txt", resume(5))]))
    (b,) = wait_batch(api, upload(api, [("b.txt", resume(5) + "Ещё строка\n".encode())]))
    (c,) = wait_batch(api, upload(api, [("c.txt", resume(5) + "Третья\n".encode())]))
    a, b, c = a["candidate_id"], b["candidate_id"], c["candidate_id"]
    assert api.get(f"/api/candidates/{b}").json()["possible_duplicate_of"] == a
    assert api.post(f"/api/candidates/{b}/dismiss-duplicate").status_code == 200
    assert api.get(f"/api/candidates/{b}").json()["possible_duplicate_of"] is None
    assert api.post(f"/api/candidates/{b}/dismiss-duplicate").status_code == 409
    assert api.post(f"/api/candidates/{a}/merge", json={"other": c}).status_code == 200
    assert api.get(f"/api/candidates/{c}").status_code == 404
    assert len(api.get(f"/api/candidates/{a}").json()["versions"]) == 2
    assert api.post(f"/api/candidates/{a}/merge", json={"other": a}).status_code == 409
    assert api.post(f"/api/candidates/{a}/merge", json={"other": 999}).status_code == 404


def zip_of(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_upload_limits_before_unpack(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_read(*_: Any, **__: Any) -> bytes:
        raise AssertionError("архив распакован")

    monkeypatch.setattr("rvm.ingest.limits.MAX_BYTES", 100)
    monkeypatch.setattr(zipfile.ZipFile, "read", no_read)
    (f,) = wait_batch(api, upload(api, [("p.zip", zip_of({"big.txt": b"x" * 200}))]))
    assert f["state"] == "error" and "big.txt" in f["message"]

    monkeypatch.setattr("rvm.ingest.limits.MAX_REQUEST_BYTES", 150)
    two = [("1.zip", zip_of({"a.txt": b"x" * 80})), ("2.zip", zip_of({"b.txt": b"x" * 80}))]
    r = api.post("/api/uploads", files=[("files", f) for f in two])
    assert r.status_code == 413  # распакованный объём запроса — по заявленным размерам
    r = api.post("/api/uploads", files=[("files", ("r.txt", b"x" * 200))])
    assert r.status_code == 413  # сырой размер — до чтения


def test_starts_in_demo_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for k in ("LLM_API_KEY", "LLM_MODE", "RVM_SECRET", "RVM_DEMO_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RVM_CACHE", str(tmp_path / "cache.sqlite"))
    with TestClient(create_app(db=tmp_path / "rvm.sqlite"), base_url="http://localhost") as c:
        assert c.get("/api/health").json()["mode"] == "demo"
        assert c.get("/api/login-options").json() == {"demo": False}
        r = c.post("/api/login", json={"role": "recruiter"}, headers=HEADERS)
        assert r.status_code == 403 and "демо-вход отключён" in r.text
    assert "LLM_API_KEY" in caplog.text and "RVM_SECRET" in caplog.text


def test_reserve_sweep_keeps_concurrent_edit(
    tmp_path: Path, api: TestClient, fake: SwitchFake
) -> None:
    from rvm import store
    from rvm.models import Status

    v1 = add_vacancy(api, "Python-разработчик\nТребования: Python")
    (f,) = wait_batch(api, upload(api, [("a.txt", resume(1))]))
    cid = f["candidate_id"]
    api.patch(f"/api/candidates/{cid}", json={"status": "reserve"})
    rid = api.get(f"/api/candidates/{cid}").json()["versions"][0]["id"]
    req = api.get(f"/api/evaluations/{rid}/{v1}").json()["result"]["requirements"][0]["id"]
    edited: list[Any] = []

    def edit() -> None:  # рекрутер правит строку, пока идёт сверка резерва
        edited.append(
            store.answer(
                tmp_path / "rvm.sqlite", rid, v1, req, Status.FULL, flag=store.BY_RECRUITER
            )
        )

    fake.full, fake.hook = True, edit
    v2 = add_vacancy(api, "Python-аналитик\nТребования: Python")
    wait(api, "/api/vacancies", lambda b: b[-1]["reserve_state"] == "done")
    assert edited
    row = api.get(f"/api/evaluations/{rid}/{v1}").json()["result"]
    assert row["score"] == edited[0].model_dump(mode="json")
    assert row["rows"][0]["flags"] == ["изменено рекрутером"]
    card = api.get(f"/api/candidates/{cid}").json()
    assert sorted(e["place"] for e in card["evaluations"]) == [1, 2]
    report = api.get("/api/evaluations", params={"candidate": cid}).json()
    scores = [v["score"]["score"] for v in report["vacancies"]]
    assert scores == sorted(scores, reverse=True)
    evaluated = [e["vacancy_id"] for e in card["events"] if e["type"] == "evaluated"]
    assert evaluated == [v1, v2]  # сверка пишет оценку только новой вакансии


def test_batch_zip_not_limited_by_docx_cap() -> None:
    from rvm.api.app import _unpack

    chunk = os.urandom(6 * 2**20).hex().encode()  # 12 МБ текста, сжатие около 2 раз
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for i in range(10):
            z.writestr(f"r{i}.txt", chunk)
    parts = _unpack("pack.zip", buf.getvalue(), 200 * 2**20)
    assert len(parts) == 10 and sum(len(d) for _, d in parts) == 120 * 2**20

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("r.txt", b"0" * 2**20)
    with pytest.raises(ValueError, match="zip-бомб"):
        _unpack("bomb.zip", bomb.getvalue(), 200 * 2**20)


def demo_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    for k in ("LLM_API_KEY", "LLM_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RVM_CACHE", str(tmp_path / "cache.sqlite"))
    monkeypatch.setenv("RVM_SECRET", "тестовый-секрет")
    monkeypatch.setenv("RVM_DEMO_LOGIN", "1")
    return create_app(db=tmp_path / "rvm.sqlite", today=date(2026, 11, 12))


def test_demo_mode_analyzes_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rvm.llm.client import DEMO_MODEL

    with client(demo_app(tmp_path, monkeypatch), "recruiter") as c:
        add_vacancy(c, "Python-разработчик\nТребования:\nОпыт разработки на Python\nЗнание SQL")
        text = "Кандидат\nОпыт разработки на Python 5 лет\nSQL, PostgreSQL\n".encode()
        (f,) = wait_batch(c, upload(c, [("r.txt", text)]))
        assert f["state"] == "done" and DEMO_MODEL in f["message"]
        report = c.get("/api/evaluations", params={"candidate": f["candidate_id"]}).json()
    assert report["model"] == DEMO_MODEL
    assert report["vacancies"] and report["vacancies"][0]["rows"]


def test_key_without_model_fails_at_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.delenv("LLM_MODE", raising=False)
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        create_app(db=tmp_path / "rvm.sqlite")


def test_explicit_demo_mode_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LLM_MODE", "demo")
    monkeypatch.setenv("LLM_API_KEY", "x")  # ключ при demo не используется
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("RVM_CACHE", str(tmp_path / "cache.sqlite"))
    with TestClient(create_app(db=tmp_path / "rvm.sqlite"), base_url="http://localhost") as c:
        assert c.get("/api/health").json()["mode"] == "demo"
    assert "LLM_API_KEY не задан" not in caplog.text


def test_delete_when_db_busy(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from rvm.store import CleanupError

    def busy(_: Path) -> None:
        raise CleanupError("журнал не очищен: база занята")

    monkeypatch.setattr("rvm.store.db.cleanup", busy)
    add_vacancy(api, "Python-разработчик\nТребования: Python")
    bid = upload(api, [("Иванов.txt", resume(1)), ("b.txt", resume(2))])
    a, _ = wait_batch(api, bid)
    r = api.delete(f"/api/candidates/{a['candidate_id']}")
    assert r.status_code == 200 and "занята" in r.json()["warning"]
    files = api.get(f"/api/uploads/{bid}").json()["files"]
    assert [f["name"] for f in files] == ["b.txt"]
    assert api.get(f"/api/candidates/{a['candidate_id']}").status_code == 404


def test_same_file_twice_in_batch(api: TestClient) -> None:
    add_vacancy(api, "Python-разработчик\nТребования: Python")
    a, b = wait_batch(api, upload(api, [("a.txt", resume(4)), ("a2.txt", resume(4))]))
    assert a["candidate_id"] == b["candidate_id"]
    card = api.get(f"/api/candidates/{a['candidate_id']}").json()
    assert [e["type"] for e in card["events"]].count("evaluated") == 1


def test_manager_sees_no_recruiter_comments(app: FastAPI, api: TestClient) -> None:
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    (f,) = wait_batch(api, upload(api, [("a.txt", resume(1))]))
    cid = f["candidate_id"]
    r = api.post(
        "/api/triage/decide",
        json={"candidate_ids": [cid], "decision": 1, "vacancy_id": vid, "comment": "секрет-р"},
    )
    assert r.status_code == 200, r.text
    api.post("/api/triage/spot", json={"candidate_id": cid, "agree": True, "comment": "секрет-с"})
    api.patch(f"/api/candidates/{cid}", json={"status": "cust"})
    with client(app, "manager") as boss:
        url = f"/api/candidates/{cid}/manager-decision"
        assert boss.post(url, json={"decision": "invite", "comment": "моё"}).status_code == 200
        api.patch(f"/api/candidates/{cid}", json={"status": "cust"})
        events = boss.get(f"/api/candidates/{cid}").json()["events"]
    assert events and {e["type"] for e in events} == {"status"}
    assert "секрет" not in str(events)
    assert any(e["details"].get("comment") == "моё" for e in events)


def test_candidates_page_filters_and_vacancy_counters(
    tmp_path: Path, app: FastAPI, api: TestClient
) -> None:
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    files = wait_batch(
        api, upload(api, [(f"{i}.txt", resume(i)) for i in range(1, 6)], applied=vid)
    )
    ids = [f["candidate_id"] for f in files]
    api.patch(f"/api/candidates/{ids[0]}", json={"status": "cust"})

    assert isinstance(api.get("/api/candidates").json(), list)  # без page — старый формат
    r = api.get("/api/candidates", params={"page": 1, "limit": 2, "offset": 1, "sort": "date"})
    body = r.json()
    assert (body["total"], body["limit"], body["offset"], len(body["items"])) == (5, 2, 1, 2)
    everyone = [c["id"] for c in api.get("/api/candidates", params={"sort": "date"}).json()]
    assert [c["id"] for c in body["items"]] == everyone[1:3]
    assert api.get("/api/candidates", params={"page": 1}).json()["limit"] == 50
    assert api.get("/api/candidates", params={"page": 1, "limit": 201}).status_code == 422
    assert api.get("/api/candidates", params={"verdict": "fit,чепуха"}).status_code == 422
    one = api.get("/api/candidates", params={"page": 1, "status": "cust", "days": 7}).json()
    assert [c["id"] for c in one["items"]] == [ids[0]] and one["total"] == 1
    assert api.get("/api/candidates", params={"page": 1, "vacancy": vid}).json()["total"] == 5
    assert api.get("/api/candidates", params={"page": 1, "source": "hh"}).json()["total"] == 0
    empty = {"page": 1, "vacancy": "", "source": "", "q": "", "verdict": ""}
    assert api.get("/api/candidates", params=empty).json()["total"] == 5  # пусто — без фильтра
    assert api.get("/api/candidates", params={"days": 3651}).status_code == 422
    # days=1 — с локальной полуночи (как «Сводка»), а не «сутки назад»
    before = (datetime.fromisoformat(period_start(1)) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(tmp_path / "rvm.sqlite") as conn:
        conn.execute("UPDATE candidates SET created_at=? WHERE id=?", (before, ids[1]))
    assert api.get("/api/candidates", params={"page": 1, "days": 1}).json()["total"] == 4
    assert api.get("/api/candidates", params={"page": 1, "days": 2}).json()["total"] == 5

    with client(app, "manager") as boss:
        page = boss.get("/api/candidates", params={"page": 1, "status": "new"}).json()
        assert [c["id"] for c in page["items"]] == [ids[0]] and page["total"] == 1

    v = next(x for x in api.get("/api/vacancies").json() if x["id"] == vid)
    assert v["applied"] == 5 and v["waiting"] == 4
    assert v["to_customer"] == 1  # PATCH без вакансии — по вакансии отклика
