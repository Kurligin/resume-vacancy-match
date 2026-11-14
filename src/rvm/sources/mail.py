"""Источник «почта» (IMAP): письма за последние SINCE_DAYS дней с вложениями-резюме.

RVM_IMAP_HOST, RVM_IMAP_USER, RVM_IMAP_PASSWORD; RVM_IMAP_FOLDER (INBOX), RVM_IMAP_SSL (1 —
IMAP4_SSL, 0 — IMAP4), RVM_IMAP_PORT (993 / 143). ref = Message-ID (без него — uid:<UID>).
Что уже забрано, решает только source_seen, а не флаг \\Seen: поиск — SINCE, а не UNSEEN.
Поэтому письмо, разбор которого прервала остановка сервиса (forget_seen), заберётся снова, хотя
уже помечено прочитанным, а письмо, которое человек открыл раньше сервиса, не пропадёт.
Письма читаются без отметки «прочитано» (BODY.PEEK); \\Seen ставится после постановки вложений
в очередь — только как подсказка человеку в почтовом клиенте. Письмо без подходящих вложений
остаётся непрочитанным и учитывается один раз как «пропущено». Письмо больше MAX_MAIL_BYTES
(RFC822.SIZE до скачивания) пропускается с причиной; вложения одного письма — одна пачка с общими лимитами. За опрос — не
больше POLL_ITEMS вложений и POLL_BYTES байт писем, остальное — в следующий опрос.
Вакансия отклика: название открытой вакансии (без регистра) в теме письма — так приходят
уведомления hh и сайта; подходят несколько — самое длинное название.
"""

import email
import imaplib
import re
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage, Message
from typing import Any, cast

from rvm.ingest import SUFFIXES, limits
from rvm.sources.base import POLL_BYTES, POLL_ITEMS, Fetched, Item

TIMEOUT_S = 30
SINCE_DAYS = 14  # сервис простоял дольше — письма старше придётся загрузить вручную
# месяцы IMAP-даты — не через strftime, чтобы не зависеть от локали
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_MAIL_BYTES = limits.MAX_REQUEST_BYTES  # сырой размер письма (вложения в base64)


def _connect(env: Mapping[str, str]) -> imaplib.IMAP4:
    ssl = env.get("RVM_IMAP_SSL", "1") != "0"
    cls = imaplib.IMAP4_SSL if ssl else imaplib.IMAP4
    port = int(env.get("RVM_IMAP_PORT") or (993 if ssl else 143))
    m = cls(env["RVM_IMAP_HOST"], port, timeout=TIMEOUT_S)
    try:
        m.login(env["RVM_IMAP_USER"], env["RVM_IMAP_PASSWORD"])
        typ, data = m.select(env.get("RVM_IMAP_FOLDER") or "INBOX")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"папка не открыта: {data}")
    except BaseException:
        m.logout()
        raise
    return m


def _fetch(m: imaplib.IMAP4, uid: bytes, what: str) -> tuple[bytes, EmailMessage]:
    """(строка ответа без литерала — там RFC822.SIZE и т. п., письмо или заголовки)."""
    typ, data = m.uid("FETCH", uid, f"({what})")
    part = next((p for p in data if isinstance(p, tuple)), None)
    if typ != "OK" or part is None:
        raise imaplib.IMAP4.error(f"письмо {uid!r} не получено")
    meta = b" ".join(p[0] if isinstance(p, tuple) else p for p in data if p)
    return meta, cast(EmailMessage, email.message_from_bytes(part[1], policy=policy.default))


def _applied(subject: str, vacancies: dict[str, dict[str, Any]]) -> str | None:
    subject = subject.casefold()
    titles = {vid: v["title"] for vid, v in vacancies.items()}
    hits = [(len(t), vid) for vid, t in titles.items() if t and t.casefold() in subject]
    return max(hits)[1] if hits else None


def _signature(part: Message) -> bool:
    """Картинка в теле письма (логотип в подписи) — не резюме."""
    return part.get_content_maintype() == "image" and part.get_content_disposition() == "inline"


def fetch(
    env: Mapping[str, str], vacancies: dict[str, dict[str, Any]], seen: Callable[[str], bool]
) -> Fetched:
    items, skipped, uids = [], [], {}
    used = 0
    m = _connect(env)
    try:
        d = date.today() - timedelta(days=SINCE_DAYS)
        # shortcut: заголовки каждого письма окна запрашиваются каждый опрос (по запросу на письмо);
        # при сотнях писем в день — сузить окно или помнить последний UID
        typ, data = m.uid("SEARCH", "SINCE", f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"поиск писем: {data}")
        for uid in data[0].split():
            meta, head = _fetch(m, uid, "RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
            ref = str(head["Message-ID"] or "").strip() or f"uid:{uid.decode()}"
            if seen(ref):
                continue
            size = re.search(rb"RFC822\.SIZE (\d+)", meta)
            if not size:
                raise imaplib.IMAP4.error(f"сервер не сообщил размер письма {uid!r}")
            if int(size[1]) > MAX_MAIL_BYTES:
                skipped.append((ref, f"письмо больше {MAX_MAIL_BYTES // 2**20} МБ"))
                continue
            if len(items) >= POLL_ITEMS or (items and used + int(size[1]) > POLL_BYTES):
                break
            used += int(size[1])
            _, msg = _fetch(m, uid, "BODY.PEEK[]")
            applied = _applied(str(msg["Subject"] or ""), vacancies)
            found = [
                Item(ref, name, part.get_payload(decode=True) or b"", applied)
                for part in msg.walk()
                if (name := part.get_filename())
                and name.lower().endswith((*SUFFIXES, ".zip"))
                and not _signature(part)
            ]
            if found:
                items += found
                uids[ref] = uid
            else:
                skipped.append((ref, "письмо без вложений-резюме"))
    finally:
        m.logout()

    def queued(refs: list[str]) -> None:
        conn = _connect(env)
        try:
            for ref in dict.fromkeys(refs):
                conn.uid("STORE", uids[ref], "+FLAGS", "(\\Seen)")
        finally:
            conn.logout()

    return Fetched(items, skipped, queued=queued)
