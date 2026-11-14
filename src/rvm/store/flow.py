"""Поток откликов: сводка по кандидату, решения пачкой, выборочная проверка, ответ отправлен."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from rvm.models import Line
from rvm.store.db import (
    _LINES,
    BEST_EVAL,
    LAST_RESUME,
    RECRUITER,
    CandidateStatus,
    _event,
    _tx,
)

UNDECIDED = (CandidateStatus.NEW, CandidateStatus.REVIEW)
_MASK = re.compile(r"(\[[^\]\n]+\])")


def summaries(db: Path, ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    """По кандидату: последняя версия резюме, должность из фактов, лучшая вакансия этой версии,
    дата ответа (None — ждёт ответа)."""
    where = f"WHERE c.id IN ({','.join('?' * len(ids))})" if ids is not None else ""
    sql = (
        "SELECT c.id, c.status, c.created_at, r.id AS resume_id,"
        " json_extract(r.analysis, '$.facts.resume.periods') AS periods,"
        " (SELECT min(at) FROM events WHERE candidate_id=c.id AND type='reply_sent') AS replied_at,"
        " b.vacancy_id AS best_vacancy, json_extract(b.result, '$.title') AS best_title,"
        " b.score AS best_vacancy_score"
        f" FROM candidates c LEFT JOIN resumes r ON r.id={LAST_RESUME}"
        " LEFT JOIN evaluations b ON b.resume_id=r.id"
        f" AND b.vacancy_id=(SELECT vacancy_id {BEST_EVAL})"
        f" {where}"
    )
    with _tx(db, write=False) as conn:
        rows = conn.execute(sql, ids or []).fetchall()
    out = {}
    for row in rows:
        periods = json.loads(row["periods"] or "[]")
        out[row["id"]] = {
            "status": row["status"],
            "created_at": row["created_at"],
            "resume_id": row["resume_id"],
            "position": next((p["title"] for p in periods if p.get("title")), None),
            "best_vacancy": row["best_vacancy"],
            "best_title": row["best_title"],
            "best_vacancy_score": row["best_vacancy_score"],
            "replied_at": row["replied_at"],
            "waiting": row["replied_at"] is None,
        }
    return out


def _name_in(original: list[Line], masked: list[Line]) -> str | None:
    """ФИО: первая строка с [ФИО] — маскированный текст как шаблон для исходной строки."""
    orig = {ln.n: ln.text for ln in original}
    for m in masked:
        if "[ФИО]" not in m.text:
            continue
        parts = _MASK.split(m.text)
        rx = "".join(re.escape(p) if i % 2 == 0 else "(.+?)" for i, p in enumerate(parts))
        hit = re.fullmatch(rx, orig.get(m.n, ""), re.DOTALL)
        if hit:
            return " ".join(g for k, g in zip(parts[1::2], hit.groups()) if k == "[ФИО]")
    return None


def full_name(db: Path, candidate_id: int) -> str | None:
    """ФИО из исходного текста последней версии — только для показа рекрутеру."""
    with _tx(db, write=False) as conn:
        r = conn.execute(
            "SELECT lines, original FROM resumes WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
    if r is None:
        return None
    return _name_in(_LINES.validate_json(r["original"]), _LINES.validate_json(r["lines"]))


def stats(db: Path) -> dict[str, Any]:
    """Счётчики шапки: всего, по статусам, ждут ответа, среднее время до ответа (дней)."""
    with _tx(db, write=False) as conn:
        rows = conn.execute(
            "SELECT c.status, c.created_at, (SELECT min(at) FROM events"
            " WHERE candidate_id=c.id AND type='reply_sent') AS replied_at FROM candidates c"
        ).fetchall()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    waits = [
        (datetime.fromisoformat(r["replied_at"]) - datetime.fromisoformat(r["created_at"]))
        for r in rows
        if r["replied_at"]
    ]
    avg = sum(w.total_seconds() for w in waits) / len(waits) / 86400 if waits else None
    return {
        "total": len(rows),
        "by_status": by_status,
        "waiting": sum(1 for r in rows if not r["replied_at"]),
        "avg_reply_days": None if avg is None else round(avg, 1),
    }


def _check(conn: Any, ids: list[int]) -> None:
    found = {
        r[0]
        for r in conn.execute(
            f"SELECT id FROM candidates WHERE id IN ({','.join('?' * len(ids))})", ids
        )
    }
    missing = sorted(set(ids) - found)
    if missing:
        raise KeyError(f"нет кандидатов {', '.join(map(str, missing))}")


def set_statuses(
    db: Path,
    ids: list[int],
    status: CandidateStatus,
    who: str = RECRUITER,
    vacancy_id: str | None = None,
    **details: Any,
) -> None:
    """Решение пачкой: все или никто (нет хоть одного кандидата — KeyError, ничего не меняется)."""
    new = CandidateStatus(status)
    with _tx(db) as conn:
        _check(conn, ids)
        for cid in ids:
            old = conn.execute("SELECT status FROM candidates WHERE id=?", (cid,)).fetchone()[0]
            conn.execute("UPDATE candidates SET status=? WHERE id=?", (str(new), cid))
            _event(
                conn, "status", cid, vacancy_id, who=who, **{"from": old, "to": str(new)}, **details
            )


def log(db: Path, type_: str, candidate_id: int, who: str = RECRUITER, **details: Any) -> None:
    """Событие по кандидату (ответ отправлен, выборочная проверка); нет кандидата — KeyError."""
    with _tx(db) as conn:
        _check(conn, [candidate_id])
        _event(conn, type_, candidate_id, details.pop("vacancy_id", None), who=who, **details)


def spot_answers(db: Path) -> dict[int, bool]:
    """Последний ответ выборочной проверки по кандидату: согласен ли рекрутер с «не подходит»."""
    with _tx(db, write=False) as conn:
        rows = conn.execute(
            "SELECT candidate_id, json_extract(details, '$.agree') AS agree FROM events"
            " WHERE type='spot_check' ORDER BY id"
        ).fetchall()
    return {r["candidate_id"]: bool(r["agree"]) for r in rows}
