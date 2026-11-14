"""Источник hh на имитации API работодателя hh.ru (httpx.MockTransport вместо сети)."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from rvm import store
from rvm.ingest import limits, load_text
from rvm.sources import REGISTRY, Inbox, Item, hh

TOKEN = "fake-hh-token-0123"
PDF = b"%PDF-1.4 resume"
Handler = Callable[[httpx.Request], httpx.Response]


def negotiation(nid: str, rid: str) -> dict[str, Any]:
    url = f"https://hh.ru/api_resume_converter/{rid}/Резюме.pdf?type=pdf"
    return {"id": nid, "resume": {"id": rid, "actions": {"download": {"pdf": {"url": url}}}}}


RESPONSES = {"111": [negotiation("n1", "r1"), negotiation("n2", "r2")], "222": []}


def hh_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["HH-User-Agent"]
    if request.url.host == "hh.ru":
        return httpx.Response(200, content=PDF)
    assert request.url.path == "/negotiations/response"
    items = RESPONSES[request.url.params["vacancy_id"]]
    return httpx.Response(200, json={"items": items, "pages": 1 if items else 0})


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = tmp_path / "rvm.sqlite"
        store.init(self.db)
        self.vac = [
            store.add_vacancy(self.db, load_text(f"Вакансия {h}", "Python"), source=f"hh:{h}")
            for h in ("111", "222")
        ]
        store.add_vacancy(self.db, load_text("Своя", "Go"))  # не с hh — не опрашивается
        self.taken: list[Item] = []
        self.requests: list[str] = []
        self.handler: Handler = hh_api
        client = httpx.Client(transport=httpx.MockTransport(self.route))
        monkeypatch.setattr(httpx, "get", client.get)
        monkeypatch.setattr(httpx, "stream", client.stream)
        monkeypatch.setattr(hh, "PAUSE_S", 0)
        source = next(s for s in REGISTRY if s.name == "hh")
        self.inbox = Inbox(
            self.db, self.take, lambda _: None, date.today, {"RVM_HH_TOKEN": TOKEN}, [source]
        )

    def route(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return self.handler(request)

    def take(self, group: list[Item], source: str) -> tuple[str, list[Any]]:
        assert source == "hh" and len(group) == 1
        self.taken += group
        return f"b{len(self.taken)}", []

    def state(self) -> dict[str, Any]:
        (st,) = self.inbox.view()
        return st


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def test_hh_responses(env: Env) -> None:
    assert env.inbox.check("hh")
    assert env.state()["last_error"] == ""
    assert [(i.ref, i.name, i.data, i.applied) for i in env.taken] == [
        ("hh:n1", "hh_r1.pdf", PDF, env.vac[0]),
        ("hh:n2", "hh_r2.pdf", PDF, env.vac[0]),
    ]
    listed = [u for u in env.requests if "/negotiations/" in u]
    assert {httpx.URL(u).params["vacancy_id"] for u in listed} == {"111", "222"}
    assert env.state()["today_items"] == 2

    env.requests.clear()
    env.inbox.check("hh")  # повторный опрос: отклики уже забраны, резюме не качаются
    assert len(env.taken) == 2 and not [u for u in env.requests if "hh.ru/api_resume" in u]


def test_hh_unauthorized(env: Env) -> None:
    env.handler = lambda r: httpx.Response(401, json={"errors": [{"type": "oauth"}]})
    env.inbox.check("hh")
    error = env.state()["last_error"]
    assert "нужен новый ключ доступа" in error and "RVM_" not in error
    assert TOKEN not in error and env.taken == []


def test_hh_rate_limit_stops_poll(env: Env) -> None:
    downloads = 0

    def limited(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        if request.url.host == "hh.ru":
            downloads += 1
            if downloads == 2:
                return httpx.Response(429)
        return hh_api(request)

    env.handler = limited
    env.inbox.check("hh")
    assert "лимит" in env.state()["last_error"]
    assert [i.ref for i in env.taken] == ["hh:n1"]  # забранное до 429 отдано
    assert not any("vacancy_id=222" in u for u in env.requests)  # дальше не опрашивали

    env.handler = hh_api
    env.inbox.check("hh")  # следующий опрос забирает остальное
    assert [i.ref for i in env.taken] == ["hh:n1", "hh:n2"] and env.state()["last_error"] == ""


def test_hh_poll_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hh, "POLL_ITEMS", 1)
    env.inbox.check("hh")
    assert [i.ref for i in env.taken] == ["hh:n1"] and env.state()["last_error"] == ""
    env.inbox.check("hh")
    assert [i.ref for i in env.taken] == ["hh:n1", "hh:n2"]


def test_hh_download_https_redirects_and_stream_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    RESPONSES["111"] = [negotiation(f"n{i}", f"r{i}") for i in range(1, 5)]
    RESPONSES["111"][0]["resume"]["actions"]["download"]["pdf"]["url"] = "http://hh.ru/r1.pdf"
    monkeypatch.setattr(limits, "MAX_BYTES", len(PDF))

    def api(request: httpx.Request) -> httpx.Response:
        rid = request.url.path.split("/")[2] if request.url.host == "hh.ru" else ""
        if rid == "r2":  # на поддомен hh по https — следуем
            return httpx.Response(302, headers={"location": "https://files.hh.ru/r2.pdf"})
        if rid == "r3":  # наружу — пропуск отклика, опрос продолжается
            return httpx.Response(302, headers={"location": "https://evil.example/r3.pdf"})
        if rid == "r4":  # больше лимита — читаем потоком и бросаем
            return httpx.Response(200, content=iter([PDF, b"x"]))
        if request.url.host == "files.hh.ru":
            return httpx.Response(200, content=PDF)
        return hh_api(request)

    env.handler = api
    try:
        env.inbox.check("hh")
    finally:
        RESPONSES["111"] = [negotiation("n1", "r1"), negotiation("n2", "r2")]
    assert env.state()["last_error"] == ""
    assert [(i.ref, i.data) for i in env.taken] == [("hh:n2", PDF)]
    why = {e["message"] for e in env.state()["recent"] if e["state"] == "skipped"}
    assert why == {"отклик без ссылки на резюме hh.ru", "резюме по ссылке вне hh.ru",
                   f"резюме больше {len(PDF) // 2**20} МБ"}  # fmt: skip
    assert not any(u.startswith("http://") or "evil" in u for u in env.requests)
