"""SQLite-база кандидатов: карточки, версии резюме, вакансии, оценки пар, журнал.

Соединение на операцию, WAL. Исходный текст резюме лежит только в resumes.original; в оценки и
журнал персональные данные не попадают."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import TypeAdapter

from rvm.models import Document, Line, MatchRow, Status
from rvm.pipeline.analyze import Report, ResumeView, VacancyResult, title_of
from rvm.privacy import Masked
from rvm.scoring import PairScore, rank_vacancies, score_pair

FROM_CANDIDATE = "со слов кандидата"
RECRUITER = "рекрутер"
_LINES = TypeAdapter(list[Line])
_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE candidates (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE contacts (  -- хэши телефона и почты для склейки дублей
    hash TEXT PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE
);
CREATE TABLE resumes (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    lines TEXT NOT NULL,      -- обезличенные строки (JSON)
    original TEXT NOT NULL,   -- исходные строки (JSON), только для показа рекрутеру
    analysis TEXT,            -- Report без резюме и вакансий: факты, переадресация, ...
    created_at TEXT NOT NULL
);
CREATE TABLE vacancies (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    doc TEXT NOT NULL,
    source TEXT NOT NULL,
    is_open INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE evaluations (
    resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    vacancy_id TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_versions TEXT NOT NULL,
    score REAL NOT NULL,
    low REAL NOT NULL,
    high REAL NOT NULL,
    verdict TEXT NOT NULL,
    place INTEGER,
    result TEXT NOT NULL,     -- VacancyResult (JSON): таблица, выводы
    created_at TEXT NOT NULL,
    PRIMARY KEY (resume_id, vacancy_id)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    candidate_id INTEGER,
    vacancy_id TEXT,
    who TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX resumes_candidate ON resumes(candidate_id);
CREATE INDEX events_candidate ON events(candidate_id);
"""


class CandidateStatus(StrEnum):
    NEW = "new"
    REVIEW = "review"
    CUST = "cust"  # у заказчика
    ALT = "alt"  # предложена другая вакансия
    INVITED = "invited"
    RESERVE = "reserve"
    REJECTED = "rejected"


class Added(NamedTuple):
    candidate_id: int
    resume_id: int
    new: bool  # False — этот файл уже был, оценки можно брать из базы


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lower(s: str | None) -> str | None:
    return s.lower() if s else s  # встроенный lower() в SQLite не знает кириллицу


@contextmanager
def _tx(db: Path, write: bool = True) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db, timeout=30, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")  # удалённое затирается нулями
        conn.create_function("ulower", 1, _lower, deterministic=True)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _event(
    conn: sqlite3.Connection,
    type_: str,
    candidate_id: int | None,
    vacancy_id: str | None = None,
    who: str = RECRUITER,
    **details: Any,
) -> None:
    conn.execute(
        "INSERT INTO events (at, type, candidate_id, vacancy_id, who, details)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), type_, candidate_id, vacancy_id, who, json.dumps(details, ensure_ascii=False)),
    )


def init(db: Path) -> None:
    """Создаёт файл и схему при старте; повторный вызов ничего не меняет."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
            conn.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version={_SCHEMA_VERSION}; COMMIT;")
    finally:
        conn.close()


# --- резюме и кандидаты ---


def add_resume(db: Path, data: bytes, doc: Document, masked: Masked, source: str = "файл") -> Added:
    """Тот же файл → та же версия; другой файл с общим телефоном или почтой → новая версия той
    же карточки; иначе новая карточка."""
    sha = hashlib.sha256(data).hexdigest()
    with _tx(db) as conn:
        row = conn.execute("SELECT id, candidate_id FROM resumes WHERE sha256=?", (sha,)).fetchone()
        if row:
            return Added(row["candidate_id"], row["id"], False)
        hashes = masked.contact_hashes
        found = conn.execute(
            f"SELECT min(candidate_id) FROM contacts WHERE hash IN ({','.join('?' * len(hashes))})",
            hashes,
        ).fetchone()[0]
        cid = found  # совпали контакты с несколькими карточками — берём старшую, не сливаем
        if cid is None:
            cid = conn.execute(
                "INSERT INTO candidates (created_at, source) VALUES (?, ?)", (_now(), source)
            ).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO contacts (hash, candidate_id) VALUES (?, ?)",
            [(h, cid) for h in hashes],
        )
        rid = conn.execute(
            "INSERT INTO resumes (candidate_id, sha256, name, status, lines, original, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                sha,
                doc.name,
                doc.status,
                _LINES.dump_json(masked.doc.lines).decode(),
                _LINES.dump_json(doc.lines).decode(),
                _now(),
            ),
        ).lastrowid
        version = conn.execute(
            "SELECT count(*) FROM resumes WHERE candidate_id=?", (cid,)
        ).fetchone()[0]
        _event(conn, "uploaded", cid, who="система", resume_id=rid, version=version)
        assert cid is not None and rid is not None
        return Added(cid, rid, True)


def save_report(db: Path, resume_id: int, report: Report) -> None:
    """Сохраняет отчёт: оценка на каждую пару (повторная оценка пары заменяет старую)."""
    name = report.resume.name
    # в предупреждениях имя файла резюме (там бывает ФИО) — заменяем
    scrub = report.model_copy(
        update={"warnings": [w.replace(name, "резюме") for w in report.warnings]}
    )
    analysis = scrub.model_dump_json(exclude={"resume", "vacancies"})
    prompts = json.dumps(report.prompt_versions)
    with _tx(db) as conn:
        cid = _candidate_of(conn, resume_id)
        conn.execute("UPDATE resumes SET analysis=? WHERE id=?", (analysis, resume_id))
        for v in report.vacancies:
            _put_eval(conn, resume_id, v, report.model, prompts)
            _event(conn, "evaluated", cid, v.id, who="система", score=v.score.score)


def _put_eval(
    conn: sqlite3.Connection, resume_id: int, v: VacancyResult, model: str, prompts: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO evaluations (resume_id, vacancy_id, model, prompt_versions,"
        " score, low, high, verdict, place, result, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            resume_id,
            v.id,
            model,
            prompts,
            v.score.score,
            v.score.low,
            v.score.high,
            v.verdict,
            v.place,
            v.model_dump_json(),
            _now(),
        ),
    )


def _candidate_of(conn: sqlite3.Connection, resume_id: int) -> int:
    row = conn.execute("SELECT candidate_id FROM resumes WHERE id=?", (resume_id,)).fetchone()
    if row is None:
        raise KeyError(f"нет резюме {resume_id}")
    return int(row[0])


def load_report(db: Path, resume_id: int) -> Report:
    """Отчёт целиком для показа рекрутеру — с исходным текстом резюме."""
    with _tx(db, write=False) as conn:
        r = conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
        if r is None or r["analysis"] is None:
            raise KeyError(f"нет отчёта по резюме {resume_id}")
        evals = conn.execute(
            "SELECT result FROM evaluations WHERE resume_id=? ORDER BY place IS NULL, place",
            (resume_id,),
        ).fetchall()
    view = ResumeView(
        name=r["name"],
        lines=_LINES.validate_json(r["lines"]),
        original=_LINES.validate_json(r["original"]),
        status=r["status"],
    )
    data = json.loads(r["analysis"])
    data["resume"] = view
    data["vacancies"] = [VacancyResult.model_validate_json(e["result"]) for e in evals]
    return Report.model_validate(data)


def list_candidates(
    db: Path,
    status: CandidateStatus | None = None,
    q: str | None = None,
    sort: Literal["score", "date"] = "score",
) -> list[dict[str, Any]]:
    """Карточки с лучшим баллом; q ищет по обезличенному тексту резюме (должность, навыки)."""
    where, args = [], []
    if status is not None:
        where.append("c.status=?")
        args.append(str(status))
    if q:
        where.append(
            "EXISTS (SELECT 1 FROM resumes r WHERE r.candidate_id=c.id"
            " AND instr(ulower(r.lines), ?) > 0)"
        )
        args.append(q.lower())
    order = "best_score IS NULL, best_score DESC" if sort == "score" else "c.created_at DESC"
    sql = (
        "SELECT c.id, c.status, c.created_at, c.source,"
        " (SELECT count(*) FROM resumes r WHERE r.candidate_id=c.id) AS versions,"
        " (SELECT max(e.score) FROM evaluations e JOIN resumes r ON r.id=e.resume_id"
        "  WHERE r.candidate_id=c.id) AS best_score"
        f" FROM candidates c {'WHERE ' + ' AND '.join(where) if where else ''}"
        f" ORDER BY {order}, c.id DESC"
    )
    with _tx(db, write=False) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def card(db: Path, candidate_id: int) -> dict[str, Any]:
    """Карточка: версии резюме, оценки по вакансиям (последней версии сверху), журнал."""
    with _tx(db, write=False) as conn:
        c = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if c is None:
            raise KeyError(f"нет кандидата {candidate_id}")
        versions = conn.execute(
            "SELECT id, sha256, name, status, created_at FROM resumes WHERE candidate_id=?"
            " ORDER BY id DESC",
            (candidate_id,),
        ).fetchall()
        evals = conn.execute(
            "SELECT e.resume_id, e.vacancy_id, e.model, e.prompt_versions, e.score, e.low, e.high,"
            " e.verdict, e.place, e.created_at, json_extract(e.result, '$.title') AS title"
            " FROM evaluations e JOIN resumes r ON r.id=e.resume_id WHERE r.candidate_id=?"
            " ORDER BY e.resume_id DESC, e.place",
            (candidate_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT at, type, vacancy_id, who, details FROM events WHERE candidate_id=?"
            " ORDER BY id",
            (candidate_id,),
        ).fetchall()
    return {
        **dict(c),
        "versions": [dict(v) for v in versions],
        "evaluations": [
            {**dict(e), "prompt_versions": json.loads(e["prompt_versions"])} for e in evals
        ],
        "events": [{**dict(e), "details": json.loads(e["details"])} for e in events],
    }


def set_status(db: Path, candidate_id: int, status: CandidateStatus, who: str = RECRUITER) -> None:
    new = CandidateStatus(status)  # неизвестный статус → ValueError
    with _tx(db) as conn:
        row = conn.execute("SELECT status FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(f"нет кандидата {candidate_id}")
        conn.execute("UPDATE candidates SET status=? WHERE id=?", (str(new), candidate_id))
        _event(conn, "status", candidate_id, who=who, **{"from": row["status"], "to": str(new)})


def answer(
    db: Path,
    resume_id: int,
    vacancy_id: str,
    requirement_id: str,
    value: Status,
    note: str = "",
    who: str = RECRUITER,
) -> PairScore:
    """Ответ кандидата на вопрос: строка получает статус с флагом «со слов кандидата», балл,
    вилка и места вакансий пересчитываются."""
    with _tx(db) as conn:
        cid = _candidate_of(conn, resume_id)
        rows = conn.execute(
            "SELECT result, model, prompt_versions FROM evaluations WHERE resume_id=?",
            (resume_id,),
        ).fetchall()
        results = {}
        for r in rows:
            v = VacancyResult.model_validate_json(r["result"])
            results[v.id] = (v, r)
        if vacancy_id not in results:
            raise KeyError(f"нет оценки резюме {resume_id} по вакансии {vacancy_id}")
        res = results[vacancy_id][0]
        if requirement_id not in {r.id for r in res.requirements}:
            raise KeyError(f"нет требования {requirement_id}")
        row = next((r for r in res.rows if r.requirement_id == requirement_id), None)
        if row is None:
            row = MatchRow(requirement_id=requirement_id, status=Status.UNKNOWN)
            res.rows.append(row)
        before = res.score.score
        row.status = Status(value)
        row.note = note
        if FROM_CANDIDATE not in row.flags:
            row.flags.append(FROM_CANDIDATE)
        res.score = score_pair(res.requirements, res.rows)
        res.verdict = res.score.verdict

        a = conn.execute("SELECT analysis FROM resumes WHERE id=?", (resume_id,)).fetchone()[0]
        analysis = json.loads(a)
        ranking = rank_vacancies(
            {vid: v.score for vid, (v, _) in results.items()}, analysis.get("applied")
        )
        for p in ranking.places:
            v, m = results[p.id]
            v.place, v.why_not_first = p.place, p.why_not_first
            _put_eval(conn, resume_id, v, m["model"], m["prompt_versions"])
        analysis["redirect"] = ranking.redirect and ranking.redirect.model_dump(mode="json")
        analysis["none_fit"] = ranking.none_fit
        conn.execute(
            "UPDATE resumes SET analysis=? WHERE id=?",
            (json.dumps(analysis, ensure_ascii=False), resume_id),
        )
        _event(
            conn,
            "answer",
            cid,
            vacancy_id,
            who=who,
            requirement=requirement_id,
            answer=str(row.status),
            before=before,
            after=res.score.score,
        )
        return res.score


def delete_candidate(db: Path, candidate_id: int, who: str = RECRUITER) -> None:
    """Стирает карточку, версии, тексты, оценки, хэши и журнал; остаётся запись «удалён»."""
    with _tx(db) as conn:
        if not conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,)).rowcount:
            raise KeyError(f"нет кандидата {candidate_id}")
        conn.execute("DELETE FROM events WHERE candidate_id=?", (candidate_id,))
        _event(conn, "deleted", candidate_id, who=who)
    # стёртые страницы ещё лежат в журнале WAL — переносим в базу и обрезаем журнал
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


# --- вакансии ---


def add_vacancy(db: Path, doc: Document, source: str = "файл") -> str:
    with _tx(db) as conn:
        vid = str(
            conn.execute(
                "INSERT INTO vacancies (title, doc, source, created_at) VALUES (?, ?, ?, ?)",
                (title_of(doc), doc.model_dump_json(), source, _now()),
            ).lastrowid
        )
        _event(conn, "vacancy_added", None, vid, source=source)
        return vid


def close_vacancy(db: Path, vacancy_id: str, who: str = RECRUITER) -> None:
    with _tx(db) as conn:
        if not conn.execute("UPDATE vacancies SET is_open=0 WHERE id=?", (vacancy_id,)).rowcount:
            raise KeyError(f"нет вакансии {vacancy_id}")
        _event(conn, "vacancy_closed", None, vacancy_id, who=who)


def list_vacancies(db: Path) -> list[dict[str, Any]]:
    with _tx(db, write=False) as conn:
        rows = conn.execute(
            "SELECT id, title, source, is_open, created_at FROM vacancies ORDER BY id"
        ).fetchall()
    return [{**dict(r), "id": str(r["id"]), "is_open": bool(r["is_open"])} for r in rows]


def open_vacancies(db: Path) -> dict[str, Document]:
    """Открытые вакансии в виде, который принимает analyze()."""
    with _tx(db, write=False) as conn:
        rows = conn.execute("SELECT id, doc FROM vacancies WHERE is_open=1 ORDER BY id").fetchall()
    return {str(r["id"]): Document.model_validate_json(r["doc"]) for r in rows}
