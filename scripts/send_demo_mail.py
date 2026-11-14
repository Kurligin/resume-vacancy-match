"""Демо-отклик по почте: письмо с резюме во вложении в ящик демо-стенда (GreenMail).

docker compose exec app python scripts/send_demo_mail.py "data/demo/Резюме А.txt" \
    --vacancy "<название открытой вакансии>"

Адрес и SMTP — из RVM_DEMO_MAIL_TO / RVM_SMTP_HOST / RVM_SMTP_PORT (по умолчанию ящик
hr@rvm.local на mail:3025 внутри сети compose). Без TLS и без входа: сервер внутренний.
"""

import argparse
import mimetypes
import os
import smtplib
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path


def build(path: Path, subject: str, to: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "candidate@demo.local"
    msg["To"] = to
    msg.set_content("Здравствуйте! Резюме во вложении.")
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("resume", type=Path, help="файл резюме")
    p.add_argument("--vacancy", default="", help="название вакансии — попадёт в тему")
    p.add_argument("--subject", help="тема целиком (вместо «Отклик на вакансию …»)")
    p.add_argument("--to", default=os.environ.get("RVM_DEMO_MAIL_TO", "hr@rvm.local"))
    p.add_argument("--host", default=os.environ.get("RVM_SMTP_HOST", "mail"))
    p.add_argument("--port", type=int, default=int(os.environ.get("RVM_SMTP_PORT", "3025")))
    a = p.parse_args(argv)
    subject = a.subject or (
        f"Отклик на вакансию «{a.vacancy}»" if a.vacancy else "Отклик на вакансию"
    )
    with smtplib.SMTP(a.host, a.port, timeout=30) as smtp:
        smtp.send_message(build(a.resume, subject, a.to))
    print(f"отправлено: {a.resume.name} → {a.to} ({subject})")


if __name__ == "__main__":
    main()
