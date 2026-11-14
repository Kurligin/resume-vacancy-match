"""SQLite-база кандидатов: карточки, версии резюме, вакансии, оценки пар, журнал.

Соединение на операцию, WAL. Исходный текст резюме лежит только в resumes.original; в оценки и
журнал персональные данные не попадают."""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import TypeAdapter

from rvm.models import Document, Line, MatchRow, Status
from rvm.pipeline.analyze import Facts, Report, ResumeView, VacancyResult, title_of
from rvm.pipeline.explain import SNAPSHOT_ID, build_checks, fallback
from rvm.privacy import Masked
from rvm.scoring import PairScore, rank_vacancies, score_pair

FROM_CANDIDATE = "со слов кандидата"
BY_RECRUITER = "изменено рекрутером"
CLARIFIED = "прояснено со слов кандидата"
RECRUITER = "рекрутер"
_LINES = TypeAdapter(list[Line])
_CHECK_ID = re.compile(rf"D\d+|{SNAPSHOT_ID}")
_CLEANUP_S = 2.0  # сколько ждать читателей, чтобы обрезать журнал после удаления
_V1 = """
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
# v2: номера карточек и резюме не переиспользуются (AUTOINCREMENT, в том числе номера из записей
# «удалён»); пометка «возможный повтор» вместо автосклейки по контакту
_V2 = """
CREATE TABLE candidates_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    possible_duplicate_of INTEGER REFERENCES candidates(id) ON DELETE SET NULL
);
INSERT INTO candidates_new (id, status, created_at, source)
    SELECT id, status, created_at, source FROM candidates;
DROP TABLE candidates;
ALTER TABLE candidates_new RENAME TO candidates;
CREATE TABLE resumes_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    lines TEXT NOT NULL,      -- обезличенные строки (JSON)
    original TEXT NOT NULL,   -- исходные строки (JSON), только для показа рекрутеру
    analysis TEXT,            -- Report без резюме и вакансий: факты, переадресация, ...
    created_at TEXT NOT NULL
);
INSERT INTO resumes_new SELECT * FROM resumes;
DROP TABLE resumes;
ALTER TABLE resumes_new RENAME TO resumes;
CREATE INDEX resumes_candidate ON resumes(candidate_id);
INSERT INTO sqlite_sequence (name, seq) SELECT 'candidates', 0
    WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='candidates');
UPDATE sqlite_sequence SET seq=max(seq, (SELECT coalesce(max(candidate_id), 0) FROM events))
    WHERE name='candidates';
"""
# v3: индексы под фильтры и сортировку списка кандидатов (evaluations(resume_id) покрыт ключом)
_V3 = """
CREATE INDEX IF NOT EXISTS candidates_status_created ON candidates(status, created_at);
CREATE INDEX IF NOT EXISTS candidates_created ON candidates(created_at);
"""
_MIGRATIONS = [_V1, _V2, _V3]  # user_version = число применённых
# последняя версия резюме кандидата c и лучшая вакансия версии r (как места в отчёте)
LAST_RESUME = "(SELECT max(id) FROM resumes WHERE candidate_id=c.id)"
BEST_EVAL = (
    "FROM evaluations WHERE resume_id=r.id ORDER BY place IS NULL, place, score DESC LIMIT 1"
)


class CandidateStatus(StrEnum):
    NEW = "new"
    REVIEW = "review"
    CUST = "cust"  # у заказчика
    ALT = "alt"  # предложена другая вакансия
    INVITED = "invited"
    RESERVE = "reserve"
    REJECTED = "rejected"
    CUST_REJECTED = "cust_rejected"  # отказ заказчика


class CleanupError(RuntimeError):
    """Удаление записано, но журнал WAL не обрезан: стёртое ещё лежит в файле журнала."""


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
        done = conn.execute("PRAGMA user_version").fetchone()[0]
        for v, script in enumerate(_MIGRATIONS[done:], done + 1):
            # без внешних ключей: пересборка таблицы иначе каскадом сотрёт зависимые строки
            conn.executescript(
                f"PRAGMA foreign_keys=OFF; BEGIN; {script} PRAGMA user_version={v}; COMMIT;"
            )
    finally:
        conn.close()


# --- резюме и кандидаты ---


def add_resume(db: Path, data: bytes, doc: Document, masked: Masked, source: str = "файл") -> Added:
    """Тот же файл → та же версия; иначе новая карточка. Общий телефон или почта с другой
    карточкой → пометка «возможный повтор», склеивает только рекрутер (merge_candidates)."""
    sha = hashlib.sha256(data).hexdigest()
    with _tx(db) as conn:
        row = conn.execute("SELECT id, candidate_id FROM resumes WHERE sha256=?", (sha,)).fetchone()
        if row:
            return Added(row["candidate_id"], row["id"], False)
        hashes = masked.contact_hashes
        dup = conn.execute(
            f"SELECT min(candidate_id) FROM contacts WHERE hash IN ({','.join('?' * len(hashes))})",
            hashes,
        ).fetchone()[0]
        cid = conn.execute(
            "INSERT INTO candidates (created_at, source, possible_duplicate_of) VALUES (?, ?, ?)",
            (_now(), source, dup),
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
        _event(conn, "uploaded", cid, who="система", resume_id=rid)
        if dup is not None:
            _event(conn, "possible_duplicate", cid, who="система", of=dup)
        assert cid is not None and rid is not None
        return Added(cid, rid, True)


def merge_candidates(db: Path, keep: int, drop: int, who: str = RECRUITER) -> None:
    """Рекрутер подтвердил «один человек»: версии, оценки, контакты и журнал drop → keep."""
    if keep == drop:
        raise ValueError("нельзя объединить карточку с самой собой")
    with _tx(db) as conn:
        n = conn.execute("SELECT count(*) FROM candidates WHERE id IN (?, ?)", (keep, drop))
        if n.fetchone()[0] < 2:
            raise KeyError(f"нет кандидата {keep} или {drop}")
        for table in ("resumes", "contacts", "events"):
            conn.execute(f"UPDATE {table} SET candidate_id=? WHERE candidate_id=?", (keep, drop))
        conn.execute(
            "UPDATE candidates SET possible_duplicate_of=NULL WHERE id=? AND possible_duplicate_of=?",
            (keep, drop),
        )
        conn.execute(
            "UPDATE candidates SET possible_duplicate_of=? WHERE possible_duplicate_of=?",
            (keep, drop),
        )
        conn.execute("DELETE FROM candidates WHERE id=?", (drop,))
        _event(conn, "merged", keep, who=who, **{"from": drop})


def dismiss_duplicate(db: Path, candidate_id: int, who: str = RECRUITER) -> None:
    """Рекрутер решил «разные люди»: пометка «возможный повтор» снимается."""
    with _tx(db) as conn:
        row = conn.execute(
            "SELECT possible_duplicate_of FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"нет кандидата {candidate_id}")
        if row[0] is None:
            raise ValueError(f"у кандидата {candidate_id} нет пометки «возможный повтор»")
        conn.execute("UPDATE candidates SET possible_duplicate_of=NULL WHERE id=?", (candidate_id,))
        _event(conn, "not_duplicate", candidate_id, who=who, of=row[0])


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
    rep = load_reports(db, [resume_id]).get(resume_id)
    if rep is None:
        raise KeyError(f"нет отчёта по резюме {resume_id}")
    return rep


def load_reports(db: Path, resume_ids: list[int], brief: bool = False) -> dict[int, Report]:
    """Отчёты пачкой за одно соединение (как load_report); без разбора — нет в ответе.
    brief — для «Разбора»: только лучшая вакансия и без текста резюме (разбор JSON дорогой)."""
    ids = json.dumps(resume_ids)
    best = f" AND vacancy_id=(SELECT vacancy_id {BEST_EVAL})" if brief else ""
    with _tx(db, write=False) as conn:
        rows = conn.execute(
            "SELECT id, name, status, analysis"
            + ("" if brief else ", lines, original")
            + " FROM resumes WHERE analysis IS NOT NULL AND id IN (SELECT value FROM json_each(?))",
            (ids,),
        ).fetchall()
        evals: dict[int, list[VacancyResult]] = {}
        for e in conn.execute(
            "SELECT r.id, e.result FROM resumes r JOIN evaluations e ON e.resume_id=r.id"
            f" WHERE r.id IN (SELECT value FROM json_each(?)){best}"
            " ORDER BY r.id, e.place IS NULL, e.place",
            (ids,),
        ):
            evals.setdefault(e[0], []).append(VacancyResult.model_validate_json(e[1]))
    out = {}
    for r in rows:
        data = json.loads(r["analysis"])
        data["resume"] = ResumeView(
            name=r["name"],
            lines=[] if brief else _LINES.validate_json(r["lines"]),
            original=[] if brief else _LINES.validate_json(r["original"]),
            status=r["status"],
        )
        data["vacancies"] = evals.get(r["id"], [])
        out[r["id"]] = Report.model_validate(data)
    return out


def _candidate_filter(
    status: CandidateStatus | None,
    q: str | None,
    vacancy: str | None,
    verdicts: list[str] | None,
    source: str | None,
    since: str | None,
) -> tuple[str, list[Any]]:
    """WHERE для списка: c — кандидат, r — его последняя версия резюме."""
    where: list[str] = []
    args: list[Any] = []
    if status is not None:
        where.append("c.status=?")
        args.append(str(status))
    if q:
        where.append(
            "EXISTS (SELECT 1 FROM resumes r2 WHERE r2.candidate_id=c.id"
            " AND instr(ulower(r2.lines), ?) > 0)"
        )
        args.append(q.lower())
    if vacancy is not None:  # отклик или лучшая вакансия последней версии
        where.append(
            f"(json_extract(r.analysis, '$.applied')=? OR (SELECT vacancy_id {BEST_EVAL})=?)"
        )
        args += [vacancy, vacancy]
    if verdicts:  # по выбранной вакансии, без неё — по лучшей
        marks = ",".join("?" * len(verdicts))
        if vacancy is not None:
            where.append(
                f"(SELECT verdict FROM evaluations WHERE resume_id=r.id AND vacancy_id=?)"
                f" IN ({marks})"
            )
            args.append(vacancy)
        else:
            where.append(f"(SELECT verdict {BEST_EVAL}) IN ({marks})")
        args += verdicts
    if source is not None:
        where.append("c.source=?")
        args.append(source)
    if since is not None:
        where.append("c.created_at>=?")
        args.append(since)
    return ("WHERE " + " AND ".join(where)) if where else "", args


def list_candidates(
    db: Path,
    status: CandidateStatus | None = None,
    q: str | None = None,
    sort: Literal["score", "date"] = "score",
    *,
    vacancy: str | None = None,
    verdicts: list[str] | None = None,
    source: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Карточки с лучшим баллом; q ищет по обезличенному тексту резюме (должность, навыки).
    Фильтры, сортировка и страница — в SQL; сколько найдено всего — count_candidates."""
    where, args = _candidate_filter(status, q, vacancy, verdicts, source, since)
    order = "best_score IS NULL, best_score DESC" if sort == "score" else "c.created_at DESC"
    sql = (
        "SELECT c.id, c.status, c.created_at, c.source,"
        " (SELECT count(*) FROM resumes v WHERE v.candidate_id=c.id) AS versions,"
        " (SELECT max(e.score) FROM evaluations e JOIN resumes v ON v.id=e.resume_id"
        "  WHERE v.candidate_id=c.id) AS best_score"
        f" FROM candidates c LEFT JOIN resumes r ON r.id={LAST_RESUME} {where}"
        f" ORDER BY {order}, c.id DESC LIMIT ? OFFSET ?"
    )
    with _tx(db, write=False) as conn:
        return [dict(r) for r in conn.execute(sql, [*args, -1 if limit is None else limit, offset])]


def count_candidates(
    db: Path,
    status: CandidateStatus | None = None,
    q: str | None = None,
    *,
    vacancy: str | None = None,
    verdicts: list[str] | None = None,
    source: str | None = None,
    since: str | None = None,
) -> int:
    """Сколько карточек под теми же фильтрами, что у list_candidates."""
    where, args = _candidate_filter(status, q, vacancy, verdicts, source, since)
    sql = f"SELECT count(*) FROM candidates c LEFT JOIN resumes r ON r.id={LAST_RESUME} {where}"
    with _tx(db, write=False) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


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


def set_status(
    db: Path, candidate_id: int, status: CandidateStatus, who: str = RECRUITER, **details: Any
) -> None:
    new = CandidateStatus(status)  # неизвестный статус → ValueError
    with _tx(db) as conn:
        row = conn.execute("SELECT status FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(f"нет кандидата {candidate_id}")
        conn.execute("UPDATE candidates SET status=? WHERE id=?", (str(new), candidate_id))
        _event(
            conn,
            "status",
            candidate_id,
            who=who,
            **{"from": row["status"], "to": str(new)},
            **details,
        )


def answer(
    db: Path,
    resume_id: int,
    vacancy_id: str,
    requirement_id: str,
    value: Status,
    note: str = "",
    who: str = RECRUITER,
    flag: str = FROM_CANDIDATE,
) -> PairScore:
    """Ответ кандидата на вопрос: строка получает статус с флагом «со слов кандидата» (или
    BY_RECRUITER — правка рекрутера), балл, вилка и места вакансий пересчитываются. Ответ на проверку (D…, SNAPSHOT) балл не меняет:
    несостыковка помечается «прояснено со слов кандидата» и уходит из рисков."""
    with _tx(db) as conn:
        cid = _candidate_of(conn, resume_id)
        results = _results(conn, resume_id)
        if vacancy_id not in results:
            raise KeyError(f"нет оценки резюме {resume_id} по вакансии {vacancy_id}")
        res = results[vacancy_id][0]
        if requirement_id not in {r.id for r in res.requirements}:
            if not _CHECK_ID.fullmatch(requirement_id):
                raise KeyError(f"нет требования {requirement_id}")
            _clarify(conn, resume_id, results, requirement_id, note)
            _event(conn, "clarified", cid, vacancy_id, who=who, check=requirement_id)
            return res.score
        row = next((r for r in res.rows if r.requirement_id == requirement_id), None)
        if row is None:
            row = MatchRow(requirement_id=requirement_id, status=Status.UNKNOWN)
            res.rows.append(row)
        before = res.score.score
        row.status = Status(value)
        row.note = note
        # флаг говорит, откуда текущий статус: прежний источник заменяется
        row.flags = [f for f in row.flags if f not in (FROM_CANDIDATE, BY_RECRUITER)] + [flag]
        res.score = score_pair(res.requirements, res.rows)
        res.verdict = res.score.verdict

        old = _analysis(conn, resume_id).get("redirect")
        _rerank(conn, resume_id, results)
        new = _analysis(conn, resume_id).get("redirect")
        # переадресация могла появиться или уйти — у вакансии отклика меняется рекомендация
        moved = {r["from_vacancy"] for r in (old, new) if r} if old != new else set()
        _reexplain(conn, resume_id, results, {vacancy_id} | moved)
        _event(
            conn,
            "answer" if flag == FROM_CANDIDATE else "edited",
            cid,
            vacancy_id,
            who=who,
            requirement=requirement_id,
            answer=str(row.status),
            before=before,
            after=res.score.score,
        )
        return res.score


def _results(conn: sqlite3.Connection, resume_id: int) -> dict[str, tuple[VacancyResult, Any]]:
    rows = conn.execute(
        "SELECT result, model, prompt_versions FROM evaluations WHERE resume_id=?", (resume_id,)
    ).fetchall()
    return {(v := VacancyResult.model_validate_json(r["result"])).id: (v, r) for r in rows}


def _rerank(
    conn: sqlite3.Connection, resume_id: int, results: dict[str, tuple[VacancyResult, Any]]
) -> None:
    """Места всех оценок резюме, переадресация и «никуда не подходит» — по текущим баллам."""
    analysis = _analysis(conn, resume_id)
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


def _analysis(conn: sqlite3.Connection, resume_id: int) -> dict[str, Any]:
    a = conn.execute("SELECT analysis FROM resumes WHERE id=?", (resume_id,)).fetchone()[0]
    return dict(json.loads(a))


def _reexplain(
    conn: sqlite3.Connection,
    resume_id: int,
    results: dict[str, tuple[VacancyResult, Any]],
    ids: set[str],
) -> None:
    """«Кратко» после ответа — по правилам из новой таблицы, без модели: балл и вердикт в
    рекомендации текущие, закрытые строки и прояснённые проверки в риски не попадают."""
    analysis = _analysis(conn, resume_id)
    f = analysis.get("facts") and Facts.model_validate(analysis["facts"])
    checks = build_checks(f.resume, f.discrepancies, f.snapshot_note) if f else []
    rd = analysis.get("redirect")
    for vid in ids:
        v, m = results[vid]
        to = rd["to_vacancy"] if rd and rd["from_vacancy"] == vid else None
        left = [c for c in checks if c.id not in v.clarified]
        v.insights = fallback(v.requirements, v.rows, v.score, left, to and results[to][0].title)
        _put_eval(conn, resume_id, v, m["model"], m["prompt_versions"])


def add_evaluation(db: Path, resume_id: int, report: Report) -> None:
    """Оценки report по новым вакансиям — к уже сохранённым (правки рекрутера в них остаются),
    места пересчитываются в той же транзакции."""
    meta = {"model": report.model, "prompt_versions": json.dumps(report.prompt_versions)}
    with _tx(db) as conn:
        cid = _candidate_of(conn, resume_id)
        results = _results(conn, resume_id) | {v.id: (v, meta) for v in report.vacancies}
        _rerank(conn, resume_id, results)
        for v in report.vacancies:
            _event(conn, "evaluated", cid, v.id, who="система", score=v.score.score)


def _clarify(
    conn: sqlite3.Connection,
    resume_id: int,
    results: dict[str, tuple[VacancyResult, sqlite3.Row]],
    check_id: str,
    note: str,
) -> None:
    """Несостыковка — про резюме, а не про вакансию: помечается во всех оценках, где она есть."""
    mark = f"{CLARIFIED}: {note}" if note else CLARIFIED
    found = False
    for v, m in results.values():
        risks = v.insights.risks if v.insights else []
        asked = v.questions.items if v.questions else []
        if check_id not in {x for p in risks for x in p.refs} | {q.requirement_id for q in asked}:
            continue
        found = True
        v.clarified[check_id] = mark
        if v.insights:  # пункт уходит, только если все его ссылки прояснены
            v.insights.risks = [p for p in risks if not set(p.refs) <= set(v.clarified)]
        _put_eval(conn, resume_id, v, m["model"], m["prompt_versions"])
    if not found:
        raise KeyError(f"нет проверки {check_id}")


def delete_candidate(db: Path, candidate_id: int, who: str = RECRUITER) -> None:
    """Стирает карточку, версии, тексты, оценки, хэши и журнал; остаётся запись «удалён»."""
    with _tx(db) as conn:
        if not conn.execute("DELETE FROM candidates WHERE id=?", (candidate_id,)).rowcount:
            raise KeyError(f"нет кандидата {candidate_id}")
        conn.execute("DELETE FROM events WHERE candidate_id=?", (candidate_id,))
        _event(conn, "deleted", candidate_id, who=who)
    cleanup(db)


def cleanup(db: Path) -> None:
    """Стёртые страницы ещё лежат в журнале WAL — переносим в базу и обрезаем журнал. Читатели
    держат журнал — повторяем до _CLEANUP_S, потом CleanupError, а не тихий успех."""
    deadline = time.monotonic() + _CLEANUP_S
    conn = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    try:
        while True:
            try:
                busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            except sqlite3.OperationalError:  # база занята писателем
                busy = 1
            if not busy:
                return
            if time.monotonic() > deadline:
                raise CleanupError(f"журнал не очищен за {_CLEANUP_S} с: база занята")
            time.sleep(0.05)
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
    """Вакансии со счётчиками по последним версиям резюме: applied — откликов, waiting — из них
    не разобрано (new/review), fit — «подходит», to_customer — у заказчика с этой вакансией
    (вакансия последней смены статуса)."""
    with _tx(db, write=False) as conn:
        rows = conn.execute(
            "SELECT id, title, source, is_open, created_at FROM vacancies ORDER BY id"
        ).fetchall()
        counts = {
            r["vid"]: dict(r)
            for r in conn.execute(
                f"""WITH last AS (SELECT c.id AS cid, c.status, {LAST_RESUME} AS rid
                    FROM candidates c),
                x AS (
                    SELECT json_extract(r.analysis, '$.applied') AS vid, 1 AS applied,
                        l.status IN ('new', 'review') AS waiting, 0 AS fit, 0 AS cust
                    FROM last l JOIN resumes r ON r.id=l.rid
                    UNION ALL
                    SELECT e.vacancy_id, 0, 0, 1, 0 FROM last l
                    JOIN evaluations e ON e.resume_id=l.rid WHERE e.verdict='fit'
                    UNION ALL
                    SELECT ev.vacancy_id, 0, 0, 0, 1 FROM last l JOIN events ev ON ev.id=(
                        SELECT max(id) FROM events WHERE candidate_id=l.cid AND type='status')
                    WHERE l.status='cust'
                )
                SELECT CAST(vid AS TEXT) AS vid, sum(applied) AS applied, sum(waiting) AS waiting,
                    sum(fit) AS fit, sum(cust) AS to_customer
                FROM x WHERE vid IS NOT NULL GROUP BY 1"""
            )
        }
    out = []
    for r in rows:
        n = counts.get(str(r["id"]), {})
        out.append(
            {**dict(r), "id": str(r["id"]), "is_open": bool(r["is_open"])}
            | {k: n.get(k, 0) for k in ("applied", "waiting", "fit", "to_customer")}
        )
    return out


def vacancy_candidates(db: Path, vacancy_id: str) -> list[dict[str, Any]]:
    """Оценки по вакансии для последней версии резюме каждого кандидата, лучшие сверху;
    blockers — тексты невыполненных обязательных требований."""
    with _tx(db, write=False) as conn:
        if not conn.execute("SELECT 1 FROM vacancies WHERE id=?", (vacancy_id,)).fetchone():
            raise KeyError(f"нет вакансии {vacancy_id}")
        rows = conn.execute(
            "SELECT c.id, c.status, e.resume_id, e.score, e.low, e.high, e.verdict, e.result"
            " FROM candidates c JOIN evaluations e"
            " ON e.resume_id=(SELECT max(id) FROM resumes WHERE candidate_id=c.id)"
            " WHERE e.vacancy_id=? ORDER BY e.score DESC, c.id",
            (vacancy_id,),
        ).fetchall()
    out = []
    for r in rows:
        res = VacancyResult.model_validate_json(r["result"])
        text = {q.id: q.text for q in res.requirements}
        out.append(
            {k: r[k] for k in ("id", "status", "resume_id", "score", "low", "high", "verdict")}
            | {"blockers": [text.get(b, b) for b in res.score.blockers]}
        )
    return out


def open_vacancies(db: Path) -> dict[str, Document]:
    """Открытые вакансии в виде, который принимает analyze()."""
    with _tx(db, write=False) as conn:
        rows = conn.execute("SELECT id, doc FROM vacancies WHERE is_open=1 ORDER BY id").fetchall()
    return {str(r["id"]): Document.model_validate_json(r["doc"]) for r in rows}
