import imaplib
import io
import os
import re
import shutil
import time
import zipfile
from collections.abc import Iterator
from concurrent.futures import Future
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest
from docx import Document as Docx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_api import EXTERNAL, SwitchFake, add_vacancy, client, resume, wait

from rvm import store
from rvm.api import create_app
from rvm.ingest import limits
from rvm.llm import LLMClient
from rvm.sources import REGISTRY, Fetched, Inbox, Item, Source, folder, mail

VACANCY = "Python-разработчик\nТребования: Python"
PASSWORD = "очень-секретный-пароль"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "тестовый-ключ-16+символов")
    monkeypatch.setenv("RVM_SECRET", "тестовый-секрет")
    monkeypatch.setenv("RVM_DEMO_LOGIN", "1")
    monkeypatch.setenv("RVM_POLL_MINUTES", "0")  # опрос в фоне в тестах не нужен
    monkeypatch.setattr(folder, "SETTLE_S", 0)  # файлы теста свежие
    for k in [
        k for k in os.environ if k.startswith(("RVM_IMAP_", "RVM_INBOX_DIR", "RVM_HH_TOKEN"))
    ]:
        monkeypatch.delenv(k)


def make_app(tmp_path: Path) -> FastAPI:
    llm = LLMClient(
        SwitchFake(), base_url=EXTERNAL, model="fake", mode="live", cache_path=tmp_path / "c.db"
    )
    return create_app(db=tmp_path / "rvm.sqlite", llm=llm, today=date(2026, 11, 12))


def source(api: TestClient, name: str) -> dict[str, Any]:
    r = api.get("/api/sources")
    assert r.status_code == 200, r.text
    return next(s for s in r.json() if s["name"] == name)


def check(api: TestClient, name: str) -> dict[str, Any]:
    before = source(api, name)["last_check"]
    r = api.post(f"/api/sources/{name}/check")
    assert r.status_code == 202 and r.json()["started"], r.text
    return wait(
        api,
        "/api/sources",
        lambda b: next(s for s in b if s["name"] == name)["last_check"] not in (None, before)
        and not next(s for s in b if s["name"] == name)["running"],
    )


def settled(api: TestClient, name: str) -> dict[str, Any]:
    """Все элементы источника разобраны."""
    body = wait(
        api,
        "/api/sources",
        lambda b: all(
            e["state"] in ("done", "error", "skipped")
            for e in next(s for s in b if s["name"] == name)["recent"]
        ),
    )
    return next(s for s in body if s["name"] == name)


# --- папка ---


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    monkeypatch.setenv("RVM_INBOX_DIR", str(path))
    return path


def test_folder(tmp_path: Path, inbox: Path) -> None:
    with client(make_app(tmp_path), "recruiter") as api:
        vid = add_vacancy(api, VACANCY)
        (inbox / f"{vid}__резюме.txt").write_bytes(resume(1))
        (inbox / "битое.docx").write_bytes(b"PK\x03\x04 not a docx")
        for skip in (".hidden.txt", "~$резюме.docx", "качается.txt.part"):
            (inbox / skip).write_bytes(resume(2))

        st = check(api, "folder")
        folder = next(s for s in st if s["name"] == "folder")
        assert folder["configured"] and folder["needs"] == [] and folder["today_items"] == 2
        wait(api, "/api/sources", lambda _: len(list((inbox / "error").iterdir())) == 1)
        wait(api, "/api/sources", lambda _: len(list((inbox / "done").iterdir())) == 1)
        folder = settled(api, "folder")
        assert {e["name"]: e["state"] for e in folder["recent"]} == {
            "резюме.txt": "done",
            "битое.docx": "error",
        }
        assert [p.name for p in (inbox / "done").iterdir()] == [f"{vid}__резюме.txt"]
        assert [p.name for p in (inbox / "error").iterdir()] == ["битое.docx"]
        left = {p.name for p in inbox.iterdir() if p.is_file()}
        assert left == {".hidden.txt", "~$резюме.docx", "качается.txt.part"}

        (cand,) = api.get("/api/candidates").json()
        assert cand["source"] == "папка"
        card = api.get(f"/api/candidates/{cand['id']}").json()
        up = next(e for e in card["events"] if e["type"] == "uploaded")
        assert up["details"]["source"] == "папка"
        assert (
            api.get("/api/evaluations", params={"candidate": cand["id"]}).json()["applied"] == vid
        )

        # тот же файл (имя, размер, время изменения) повторно не забирается и не лежит вечно
        shutil.copy2(inbox / "done" / f"{vid}__резюме.txt", inbox / f"{vid}__резюме.txt")
        again = next(s for s in check(api, "folder") if s["name"] == "folder")
        assert again["today_items"] == 2 and not (inbox / f"{vid}__резюме.txt").exists()
        assert len(list((inbox / "done").iterdir())) == 2
        assert again["recent"][0]["message"] == f"{vid}__резюме.txt: уже забран"


def test_folder_without_vacancy_prefix(tmp_path: Path, inbox: Path) -> None:
    with client(make_app(tmp_path), "recruiter") as api:
        add_vacancy(api, VACANCY)
        (inbox / "999__резюме.txt").write_bytes(resume(3))  # нет такой вакансии
        check(api, "folder")
        (entry,) = settled(api, "folder")["recent"]
        assert entry["vacancy"] is None and entry["name"] == "999__резюме.txt"


def test_folder_symlink_too_big_and_poll_limit(
    tmp_path: Path, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_bytes(resume(4))
    (inbox / "ссылка.txt").symlink_to(outside)
    (inbox / "a.txt").write_bytes(resume(1))
    (inbox / "b.txt").write_bytes(resume(2))
    (inbox / "0-большой.txt").write_bytes(b"x" * 500)  # раньше a.txt по порядку
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 400)
    monkeypatch.setattr(folder, "POLL_ITEMS", 1)
    with client(make_app(tmp_path), "recruiter") as api:
        st = next(s for s in check(api, "folder") if s["name"] == "folder")
        assert (st["today_items"], st["today_skipped"]) == (1, 1)  # лимит опроса: один файл
        assert (inbox / "error" / "0-большой.txt").exists() and (inbox / "b.txt").exists()
        st = next(s for s in check(api, "folder") if s["name"] == "folder")
        assert (st["today_items"], st["today_skipped"]) == (2, 1)
        skip = next(e for e in settled(api, "folder")["recent"] if e["state"] == "skipped")
        assert "больше" in skip["message"]
        check(api, "folder")
        assert (inbox / "ссылка.txt").is_symlink() and outside.exists()  # ссылка не тронута
        assert len(api.get("/api/candidates").json()) == 2


def test_folder_waits_until_file_settles(
    tmp_path: Path, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(folder, "SETTLE_S", 10)
    fresh = inbox / "дописывается.txt"
    fresh.write_bytes(resume(1))
    got = folder.fetch({"RVM_INBOX_DIR": str(inbox)}, {}, lambda _: False)
    assert got.items == [] and fresh.exists()  # изменён только что — ждём
    os.utime(fresh, (time.time() - 11, time.time() - 11))
    got = folder.fetch({"RVM_INBOX_DIR": str(inbox)}, {}, lambda _: False)
    assert [i.name for i in got.items] == ["дописывается.txt"]


def test_folder_empty_zip_is_error(tmp_path: Path, inbox: Path) -> None:
    buf = io.BytesIO()
    zipfile.ZipFile(buf, "w").close()
    (inbox / "пусто.zip").write_bytes(buf.getvalue())
    with client(make_app(tmp_path), "recruiter") as api:
        check(api, "folder")
        wait(api, "/api/sources", lambda _: (inbox / "error" / "пусто.zip").exists())
        (entry,) = settled(api, "folder")["recent"]
        assert entry["state"] == "error" and entry["message"] == "в архиве нет резюме"
        assert list((inbox / "done").iterdir()) == []


def test_folder_in_flight_and_log_without_path(
    inbox: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (inbox / "a.txt").write_bytes(resume(1))
    env = {"RVM_INBOX_DIR": str(inbox)}
    got = folder.fetch(env, {}, lambda _: False)
    again = folder.fetch(env, {}, lambda _: True)  # ref уже в source_seen, файл ещё в разборе
    assert again.items == again.skipped == [] and (inbox / "a.txt").exists()
    (inbox / "a.txt").unlink()  # перенос после разбора не удастся
    assert got.done
    got.done(got.items[0], True)
    assert "a.txt" in caplog.text and str(inbox) not in caplog.text


# --- почта ---


def docx_bytes() -> bytes:
    d = Docx()
    for line in resume(5).decode().splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def eml(mid: str, subject: str, attach: bytes | None) -> bytes:
    m = EmailMessage()
    m["Message-ID"], m["Subject"], m["From"] = mid, subject, "hr@example.org"
    m.set_content("Здравствуйте, резюме во вложении.")
    if attach:
        m.add_attachment(attach, *DOCX.split("/"), filename="Резюме Иванова.docx")
    return m.as_bytes()


class FakeIMAP:
    error = imaplib.IMAP4.error
    mails: ClassVar[dict[bytes, bytes]] = {}
    flagged: ClassVar[list[bytes]] = []
    opened: ClassVar[list[tuple[str, int]]] = []
    fetched: ClassVar[list[tuple[bytes, str]]] = []
    fail: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        FakeIMAP.opened.append((host, port))

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if self.fail:
            raise self.fail
        assert password == PASSWORD
        return "OK", [b"ok"]

    def select(self, folder: str) -> tuple[str, list[bytes]]:
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, cmd: str, *args: Any) -> tuple[str, list[Any]]:
        if cmd == "SEARCH":  # окно по дате, а не UNSEEN: уже забранное отсекает source_seen
            assert args[0] == "SINCE" and re.fullmatch(r"\d{1,2}-[A-Z][a-z]{2}-\d{4}", args[1])
            return "OK", [b" ".join(self.mails)]
        if cmd == "FETCH":
            mail = self.mails[args[0]]
            self.fetched.append((args[0], args[1]))
            size = b" RFC822.SIZE %d" % len(mail) if "RFC822.SIZE" in args[1] else b""
            return "OK", [(b"1 (UID " + args[0] + size + b" BODY[] {1}", mail), b")"]
        assert cmd == "STORE" and args[1:] == ("+FLAGS", "(\\Seen)")
        self.flagged.append(args[0])
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]


@pytest.fixture
def imap(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeIMAP]]:
    for k, v in {
        "RVM_IMAP_HOST": "imap.example.org",
        "RVM_IMAP_USER": "hr",
        "RVM_IMAP_PASSWORD": PASSWORD,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(imaplib, "IMAP4", FakeIMAP)
    FakeIMAP.mails = {
        b"1": eml("<a@x>", "Отклик на вакансию «PYTHON-разработчик»", docx_bytes()),
        b"2": eml("<b@x>", "Вопрос без резюме", None),
    }
    FakeIMAP.flagged, FakeIMAP.opened, FakeIMAP.fetched, FakeIMAP.fail = [], [], [], None
    yield FakeIMAP


def test_mail(tmp_path: Path, imap: type[FakeIMAP]) -> None:
    with client(make_app(tmp_path), "recruiter") as api:
        vid = add_vacancy(api, VACANCY)
        st = next(s for s in check(api, "mail") if s["name"] == "mail")
        assert st["last_error"] == "" and st["today_items"] == 1 and st["today_skipped"] == 1
        assert imap.flagged == [b"1"]  # письмо без вложений остаётся непрочитанным
        assert imap.opened[0] == ("imap.example.org", 993)
        mail = settled(api, "mail")
        got = {e["state"]: e for e in mail["recent"]}
        assert got["done"]["name"] == "Резюме Иванова.docx" and got["done"]["vacancy"] == vid
        assert got["skipped"]["message"] == "письмо без вложений-резюме"

        (cand,) = api.get("/api/candidates").json()
        assert cand["source"] == "почта"
        assert got["done"]["candidate_id"] == cand["id"] and got["skipped"]["candidate_id"] is None
        assert mail["account"] == "hr"  # логин ящика — для подписи «Почта · …», без пароля
        assert (
            api.get("/api/evaluations", params={"candidate": cand["id"]}).json()["applied"] == vid
        )

        again = next(s for s in check(api, "mail") if s["name"] == "mail")
        assert (again["today_items"], again["today_skipped"]) == (1, 1)
        assert imap.flagged == [b"1"]


def test_mail_plain_port_and_error(
    tmp_path: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RVM_IMAP_SSL", "0")
    imap.fail = imaplib.IMAP4.error(f"LOGIN failed for hr/{PASSWORD}")
    with client(make_app(tmp_path), "recruiter") as api:
        st = next(s for s in check(api, "mail") if s["name"] == "mail")
        assert imap.opened == [("imap.example.org", 143)]
        assert "LOGIN failed" in st["last_error"] and st["today_items"] == 0
        assert PASSWORD not in api.get("/api/sources").text
        assert api.get("/api/health").status_code == 200  # сервис жив


def test_mail_too_big_and_poll_limit(
    tmp_path: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    imap.mails = {
        b"1": eml("<a@x>", "Резюме", docx_bytes()),
        b"2": eml("<b@x>", "Резюме", docx_bytes()),
        b"3": eml("<c@x>", "Огромное", docx_bytes() + b"x" * 50_000),
    }
    monkeypatch.setattr(mail, "MAX_MAIL_BYTES", len(imap.mails[b"2"]) + 1000)
    monkeypatch.setattr(mail, "POLL_ITEMS", 1)
    with client(make_app(tmp_path), "recruiter") as api:
        st = next(s for s in check(api, "mail") if s["name"] == "mail")
        assert (st["today_items"], st["today_skipped"], imap.flagged) == (1, 0, [b"1"])
        st = next(s for s in check(api, "mail") if s["name"] == "mail")
        assert (st["today_items"], st["today_skipped"], imap.flagged) == (2, 1, [b"1", b"2"])
        skip = next(e for e in st["recent"] if e["state"] == "skipped")
        assert "письмо больше" in skip["message"]
        assert (b"2", "(BODY.PEEK[])") in imap.fetched
        assert (b"3", "(BODY.PEEK[])") not in imap.fetched  # большое письмо не скачивали


# --- API ---


def test_sources_api(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with client(app, "recruiter") as api:
        body = api.get("/api/sources").json()
        assert {s["name"]: s["configured"] for s in body} == {
            "mail": False,
            "hh": False,
            "folder": False,
        }
        mail = next(s for s in body if s["name"] == "mail")
        assert mail["needs"] == ["RVM_IMAP_HOST", "RVM_IMAP_USER", "RVM_IMAP_PASSWORD"]
        assert mail["recent"] == [] and mail["last_check"] is None
        assert mail["account"] == "" and mail["every_minutes"] == 0  # опрос в тестах выключен
        assert api.post("/api/sources/mail/check").status_code == 409
        assert api.post("/api/sources/нет/check").status_code == 404
        no_csrf = TestClient(app, base_url="http://localhost", cookies=api.cookies)
        assert no_csrf.post("/api/sources/folder/check").status_code == 403
    with client(app, "manager") as boss:
        assert boss.get("/api/sources").status_code == 403


def test_mail_interrupted_is_taken_again(
    tmp_path: Path, imap: type[FakeIMAP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Письмо уже помечено \\Seen, но разбор отменён остановкой сервиса — заберётся снова."""
    db = tmp_path / "rvm.sqlite"
    store.init(db)
    taken: list[str] = []

    def take(group: list[Item], source: str) -> tuple[str, list[Future[Any]]]:
        taken.append(group[0].ref)
        f: Future[Any] = Future()
        f.cancel()  # пул остановлен до разбора
        return "b", [f]

    src = next(s for s in REGISTRY if s.name == "mail")
    inbox = Inbox(db, take, lambda _: None, date.today, dict(os.environ), [src])
    inbox.check("mail")
    assert taken == ["<a@x>"] and imap.flagged == [b"1"]
    inbox.check("mail")
    assert taken == ["<a@x>", "<a@x>"]


def test_seen_marked_after_each_take(tmp_path: Path) -> None:
    db = tmp_path / "rvm.sqlite"
    store.init(db)

    def fetch(*_: Any) -> Fetched:
        return Fetched([Item("r1", "a.txt", b"1", None), Item("r2", "b.txt", b"2", None)], [])

    def take(group: list[Item], source: str) -> tuple[str, list[Future[Any]]]:
        if group[0].ref == "r2":
            raise RuntimeError("база занята")
        return "b", []

    inbox = Inbox(db, take, lambda _: None, date.today, {}, [Source("t", "T", "t", (), fetch)])
    inbox.check("t")
    assert store.seen(db, "t", "r1") and not store.seen(db, "t", "r2")
    assert "база занята" in inbox.view()[0]["last_error"]


@pytest.mark.parametrize(("raw", "every"), [("пять", 5), ("nan", 5), ("0.1", 1), ("2", 2)])
def test_poll_minutes(tmp_path: Path, inbox: Path, monkeypatch: pytest.MonkeyPatch,
                      raw: str, every: float) -> None:  # fmt: skip
    monkeypatch.setenv("RVM_POLL_MINUTES", raw)
    with client(make_app(tmp_path), "recruiter") as api:
        assert source(api, "folder")["every_minutes"] == every
