"""Источник «почта» (IMAP): непрочитанные письма с вложениями-резюме.

RVM_IMAP_HOST, RVM_IMAP_USER, RVM_IMAP_PASSWORD; RVM_IMAP_FOLDER (INBOX), RVM_IMAP_SSL (1 —
IMAP4_SSL, 0 — IMAP4), RVM_IMAP_PORT (993 / 143). Письма читаются без отметки «прочитано»
(BODY.PEEK); флаг \\Seen ставится после постановки вложений в очередь. Письмо без подходящих
вложений остаётся непрочитанным и учитывается один раз как «пропущено».
ref = Message-ID (без него — uid:<UID>).
Вакансия отклика: название открытой вакансии (без регистра) в теме письма — так приходят
уведомления hh и сайта; подходят несколько — самое длинное название.
"""

import email
import imaplib
from collections.abc import Callable, Mapping
from email import policy
from email.message import EmailMessage, Message
from typing import cast

from rvm.ingest import SUFFIXES
from rvm.sources.base import Fetched, Item

TIMEOUT_S = 30


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


def _body(m: imaplib.IMAP4, uid: bytes, what: str) -> EmailMessage:
    typ, data = m.uid("FETCH", uid, f"({what})")
    part = next((p for p in data if isinstance(p, tuple)), None)
    if typ != "OK" or part is None:
        raise imaplib.IMAP4.error(f"письмо {uid!r} не получено")
    return cast(EmailMessage, email.message_from_bytes(part[1], policy=policy.default))


def _applied(subject: str, vacancies: dict[str, str]) -> str | None:
    subject = subject.casefold()
    hits = [(len(t), vid) for vid, t in vacancies.items() if t and t.casefold() in subject]
    return max(hits)[1] if hits else None


def _signature(part: Message) -> bool:
    """Картинка в теле письма (логотип в подписи) — не резюме."""
    return part.get_content_maintype() == "image" and part.get_content_disposition() == "inline"


def fetch(
    env: Mapping[str, str], vacancies: dict[str, str], seen: Callable[[str], bool]
) -> Fetched:
    items, skipped, uids = [], [], {}
    m = _connect(env)
    try:
        typ, data = m.uid("SEARCH", "UNSEEN")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"поиск писем: {data}")
        for uid in data[0].split():
            head = _body(m, uid, "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
            ref = str(head["Message-ID"] or "").strip() or f"uid:{uid.decode()}"
            if seen(ref):
                continue
            msg = _body(m, uid, "BODY.PEEK[]")
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
        # shortcut: письмо, поставленное в очередь, но не отмеченное из-за сбоя здесь, остаётся
        # непрочитанным и уже не забирается (ref в source_seen) — разбирать вручную
        conn = _connect(env)
        try:
            for ref in dict.fromkeys(refs):
                conn.uid("STORE", uids[ref], "+FLAGS", "(\\Seen)")
        finally:
            conn.logout()

    return Fetched(items, skipped, queued=queued)
