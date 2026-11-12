import io
import os
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_analyze import RoutingFake

import rvm
from rvm.api import create_app
from rvm.llm import LLMClient

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")
EXTERNAL = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")


class SwitchFake(RoutingFake):
    """full=True — сопоставление отвечает «выполнено» по всем требованиям."""

    full = False

    def _create(self, **kwargs: Any) -> Any:
        reply = super()._create(**kwargs)
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


@pytest.fixture
def api(tmp_path: Path, fake: SwitchFake) -> Any:
    llm = LLMClient(
        fake, base_url=EXTERNAL, model="fake", mode="live", cache_path=tmp_path / "c.db"
    )
    app = create_app(db=tmp_path / "rvm.sqlite", llm=llm, today=date(2026, 11, 12))
    with TestClient(app) as client:
        yield client


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
    card = api.get(f"/api/candidates/{cid}").json()
    assert {e["type"] for e in card["events"]} >= {"uploaded", "evaluated", "answer", "status"}

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
