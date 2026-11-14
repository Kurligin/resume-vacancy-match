"""Демо-отклик по почте: письмо с темой-вакансией и резюме-вложением, без сети."""

import email
import importlib.util
from email import policy
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESUME = ROOT / "data" / "demo" / "Резюме А.txt"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "send_demo_mail", ROOT / "scripts" / "send_demo_mail.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSMTP:
    sent: ClassVar[list[tuple[str, int, EmailMessage]]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.addr = (host, port)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append((*self.addr, msg))


def test_mail_carries_vacancy_and_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _script()
    FakeSMTP.sent = []
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.delenv("RVM_SMTP_HOST", raising=False)
    monkeypatch.delenv("RVM_SMTP_PORT", raising=False)
    mod.main([str(RESUME), "--vacancy", "Аналитик данных"])

    [(host, port, msg)] = FakeSMTP.sent
    assert (host, port) == ("mail", 3025)
    # как его увидит источник «почта»: байты письма → разбор → вложения с именем файла
    got = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert got["Subject"] == "Отклик на вакансию «Аналитик данных»"
    assert got["To"] == "hr@rvm.local"
    [part] = [p for p in got.walk() if p.get_filename()]
    assert part.get_filename() == "Резюме А.txt"
    assert part.get_payload(decode=True) == RESUME.read_bytes()
