"""XSS в интерфейсе, CSP и ПД для руководителя."""

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import test_api
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_api import (
    add_vacancy,
    client,
    resume,
    upload,
    wait_batch,
)

import rvm.store

_salt, fake, app, api = test_api._salt, test_api.fake, test_api.app, test_api.api
APP_JS = Path(__file__).parents[1] / "src/rvm/api/static/app.js"
TRAP = "<img src=x onerror=alert(1)>"
CHROME = (
    shutil.which("google-chrome")
    or shutil.which("chromium")
    or next(
        (
            p
            for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
            if Path(p).exists()
        ),
        None,
    )
)
# текст из API: поля и функции, которые возвращают данные резюме/вакансий
RISKY = re.compile(
    r"\.(title|text|name|message|position|quote|comment|note|check|requirement|kind|block|why"
    r"|best_title|vacancy_id|to|answer|recommendation)\b(?!\s*(\?|===|!==|&&|\|\||\.length|\.startsWith))"
    r"|\b(title|whyShort|nameText)\("
)
# проверены вручную: EVENT экранирует данные внутри и возвращает разметку
ALLOWED = {"EVENT[e.type](e.details, title(e.vacancy_id))"}
SAFE_CALL = re.compile(r"\b(esc|cname|encodeURIComponent)\(")


def _close(s: str, i: int, open_: str, close: str) -> int:
    """Индекс парной закрывающей скобки для открывающей в позиции i."""
    depth = 0
    for j in range(i, len(s)):
        depth += {open_: 1, close: -1}.get(s[j], 0)
        if depth == 0:
            return j
    raise AssertionError(f"нет пары для {s[i:i + 40]}")


def _strip_safe(expr: str) -> str:
    """Убирает вызовы esc(...)/cname(...)/encodeURIComponent(...) вместе с аргументами."""
    while m := SAFE_CALL.search(expr):
        end = _close(expr, m.end() - 1, "(", ")")
        expr = expr[: m.start()] + "_" + expr[end + 1 :]
    return expr


def leaves(js: str) -> list[str]:
    """Выражения ${...} в шаблонных строках без вложенных шаблонов."""
    out = []
    for m in re.finditer(r"\$\{", js):
        expr = js[m.end() : _close(js, m.end() - 1, "{", "}")]
        if "${" not in expr and "`" not in expr:
            out.append(expr)
    return out


def test_app_js_escapes_api_text() -> None:
    js = APP_JS.read_text()
    html_js = "\n".join(ln for ln in js.splitlines() if "toast(" not in ln)  # тосты — текст
    raw = [e for e in leaves(html_js) if e not in ALLOWED and RISKY.search(_strip_safe(e))]
    assert raw == [], f"данные без esc(): {raw}"
    # тосты — через textContent: экранирование и HTML-имя кандидата там не нужны
    assert "span.textContent = text" in js
    toasts = re.findall(r"toast\((.*)", js)
    assert [t for t in toasts if "esc(" in t or "cname(" in t] == []


def test_static_check_catches_raw() -> None:
    assert RISKY.search(_strip_safe("c.position"))
    assert RISKY.search(_strip_safe("title(rd.to)"))
    assert not RISKY.search(_strip_safe('esc(c.position ?? "")'))
    assert not RISKY.search(_strip_safe("cname(c.id)"))
    assert not RISKY.search(_strip_safe('c.position ? "a" : ""'))


def test_csp_header(app: FastAPI) -> None:
    with TestClient(app, base_url="http://localhost") as anon:
        for path in ("/", "/app.js", "/app.css", "/api/me"):
            csp = anon.get(path).headers["content-security-policy"]
            script = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
            assert "'self'" in script and "unsafe" not in script, csp
            assert "object-src 'none'" in csp


def _sent(api: TestClient, vid: str, names: bool, i: int = 1) -> tuple[int, int]:
    (f,) = wait_batch(api, upload(api, [("Петров_резюме.txt", resume(i))]))
    cid = f["candidate_id"]
    body = {"vacancy_id": vid, "candidate_ids": [cid], "names": names}
    assert api.post("/api/package", json=body).status_code == 200
    return cid, api.get(f"/api/candidates/{cid}").json()["versions"][0]["id"]


def test_manager_gets_masked_text_and_name_only_by_flag(
    app: FastAPI, api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rvm.store, "full_name", lambda db, cid: TRAP)
    vid = add_vacancy(api, "Python-разработчик\nТребования: Python")
    hidden, rid_h = _sent(api, vid, names=False)
    shown, rid_s = _sent(api, vid, names=True, i=2)
    events = api.get(f"/api/candidates/{shown}").json()["events"]
    assert events[-1]["details"]["package"] and events[-1]["details"]["names"] is True

    rec = api.get("/api/evaluations", params={"candidate": hidden}).json()["resume"]
    assert rec["original"] != rec["lines"]  # рекрутер видит исходный текст
    assert api.get(f"/api/candidates/{hidden}?pii=true").json()["name"] == TRAP  # как есть

    with client(app, "manager") as boss:
        names = {c["id"]: c["name"] for c in boss.get("/api/candidates?pii=true").json()}
        assert names == {hidden: None, shown: TRAP}
        card = boss.get(f"/api/candidates/{hidden}?pii=true").json()
        assert card["name"] is None and card["versions"][0]["name"] == ""
        assert boss.get(f"/api/candidates/{shown}").json()["name"] == TRAP
        for cid, rid in ((hidden, rid_h), (shown, rid_s)):
            for res in (
                boss.get("/api/evaluations", params={"candidate": cid}).json()["resume"],
                boss.get(f"/api/evaluations/{rid}/{vid}").json()["resume"],
            ):
                assert res["original"] == res["lines"]
                assert "916" not in str(res)
                assert (res["name"] != "") == (cid == shown)


@pytest.mark.skipif(CHROME is None, reason="нет Chrome")
def test_trap_name_renders_as_text(
    app: FastAPI,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    free_tcp_port: int,
) -> None:
    """Руководитель с пакетом «ФИО в документе»: имя-ловушка в списке и карточке — текстом."""
    monkeypatch.setattr(rvm.store, "full_name", lambda db, cid: TRAP)
    vid = add_vacancy(api, f"{TRAP} Python-разработчик\nТребования: Python")
    cid, _ = _sent(api, vid, names=True)
    with client(app, "manager") as boss:
        cookie = boss.cookies.get("rvm_session")

    async def with_cookie(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":  # сессия руководителя без экрана входа
            scope["headers"] = [*scope["headers"], (b"cookie", f"rvm_session={cookie}".encode())]
        await app(scope, receive, send)

    port = free_tcp_port
    server = uvicorn.Server(uvicorn.Config(with_cookie, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "сервер не поднялся"
        dom = subprocess.run(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--user-data-dir=" + str(tmp_path / "chrome"),
                "--virtual-time-budget=8000",
                "--dump-dom",
                f"http://localhost:{port}/#/candidates/{cid}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    finally:
        server.should_exit = True
    assert "Кандидаты" in dom and dom.count("&lt;img src=x onerror=alert(1)&gt;") >= 2, dom[-3000:]
    assert "<img" not in dom
