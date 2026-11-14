"""Синтетическая нагрузка: отдельная база SQLite с N кандидатами — копиями разборов демо-данных.

    uv run python eval/load_seed.py [--n 5000] [--db ПУТЬ]

Демо-вакансии и резюме загружаются как при RVM_SEED=1 (scripts/start.py: seed — ответы модели
из data/demo/cache.sqlite, сеть не нужна), затем разборы клонируются SQL-вставками без модели:
даты равномерно за 30 дней, статусы и источники по долям, журнал (загрузка, оценка, смена
статуса, ответ отправлен). Тексты — те же обезличенные строки демо, новых ПДн нет.
random.Random(42): база воспроизводима. База — только отдельный файл (по умолчанию во временной
папке), не var/ и не data/."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rvm.api import auth
from rvm.store import CandidateStatus

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
DAYS = 30
# доли: большинство не разобрано или отказ, часть у заказчика, резерв, другая вакансия
STATUSES: dict[CandidateStatus, int] = {
    CandidateStatus.NEW: 40,
    CandidateStatus.REVIEW: 5,
    CandidateStatus.REJECTED: 25,
    CandidateStatus.CUST: 8,
    CandidateStatus.CUST_REJECTED: 3,
    CandidateStatus.INVITED: 3,
    CandidateStatus.RESERVE: 10,
    CandidateStatus.ALT: 6,
}
SOURCES = {"файл": 40, "почта": 25, "hh": 25, "папка": 10}  # значения candidates.source
REPLY_SHARE = 0.7  # кому из получивших решение уже отправлен ответ
SYSTEM = "система"


def _start_module() -> object:
    spec = importlib.util.spec_from_file_location("rvm_start", ROOT / "scripts" / "start.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_path(db: Path) -> Path:
    """Только новый файл вне var/ и data/ репозитория — рабочую и демо-базу не трогаем."""
    db = db.resolve()
    for forbidden in (ROOT / "var", ROOT / "data"):
        if db.is_relative_to(forbidden):
            raise SystemExit(f"{db}: синтетическая база не кладётся в {forbidden}")
    if db.exists():
        raise SystemExit(f"{db}: файл уже есть — укажите новый путь")
    return db


def seed(db: Path, n: int, rng: random.Random | None = None) -> int:
    """Демо-данные через scripts/start.py, затем n клонов их разборов. Возвращает n."""
    rng = rng or random.Random(42)
    db = check_path(db)
    os.environ.setdefault("RVM_HASH_SALT", "load-seed-" + "0" * 32)  # только для этой базы
    os.environ.setdefault("RVM_SECRET", "load-seed-" + "1" * 32)
    start = _start_module()
    start.seed(db, DEMO, db.parent / f"{db.stem}-cache.sqlite")  # type: ignore[attr-defined]
    recruiter = auth.demo_user("recruiter").who
    manager = auth.demo_user("manager").who
    now = datetime.now(UTC)
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        templates = conn.execute(
            "SELECT id, name, status, lines, original, analysis FROM resumes"
            " WHERE analysis IS NOT NULL ORDER BY id"
        ).fetchall()
        evals = {
            t[0]: conn.execute(
                "SELECT vacancy_id, model, prompt_versions, score, low, high, verdict, place,"
                " result FROM evaluations WHERE resume_id=? ORDER BY place IS NULL, place",
                (t[0],),
            ).fetchall()
            for t in templates
        }
        if not templates or not all(evals.values()):
            raise SystemExit("демо-резюме не разобраны — клонировать нечего")
        # демо-карточки заменяются клонами: в базе ровно n кандидатов
        conn.execute("DELETE FROM events WHERE candidate_id IS NOT NULL")
        conn.execute("DELETE FROM candidates")

        def event(
            at: datetime, type_: str, cid: int, vid: str | None, who: str, **d: object
        ) -> None:
            conn.execute(
                "INSERT INTO events (at, type, candidate_id, vacancy_id, who, details)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (at.isoformat(), type_, cid, vid, who, json.dumps(d, ensure_ascii=False)),
            )

        statuses, sources = list(STATUSES), list(SOURCES)
        for i in range(n):
            t = rng.choice(templates)
            ev = evals[t[0]]
            created = now - timedelta(seconds=rng.uniform(0, DAYS * 86400))
            status = rng.choices(statuses, weights=list(STATUSES.values()))[0]
            source = rng.choices(sources, weights=list(SOURCES.values()))[0]
            cid = conn.execute(
                "INSERT INTO candidates (status, created_at, source) VALUES (?, ?, ?)",
                (str(status), created.isoformat(), source),
            ).lastrowid
            assert cid is not None
            sha = hashlib.sha256(f"load-{i}".encode()).hexdigest()
            rid = conn.execute(
                "INSERT INTO resumes (candidate_id, sha256, name, status, lines, original,"
                " analysis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, sha, t[1], t[2], t[3], t[4], t[5], created.isoformat()),
            ).lastrowid
            event(created, "uploaded", cid, None, SYSTEM, resume_id=rid)
            done = created + timedelta(seconds=rng.uniform(20, 90))  # разбор ~40 с
            for e in ev:
                conn.execute(
                    "INSERT INTO evaluations (resume_id, vacancy_id, model, prompt_versions,"
                    " score, low, high, verdict, place, result, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, *e, done.isoformat()),
                )
                event(done, "evaluated", cid, e[0], SYSTEM, score=e[3])
            if status == CandidateStatus.NEW:
                continue
            decided = min(now, done + timedelta(hours=rng.uniform(1, 5 * 24)))
            # другая вакансия — вторая по месту, иначе лучшая
            vid = ev[1][0] if status == CandidateStatus.ALT and len(ev) > 1 else ev[0][0]
            first = (
                CandidateStatus.CUST
                if status in (CandidateStatus.CUST_REJECTED, CandidateStatus.INVITED)
                else status
            )
            event(decided, "status", cid, vid, recruiter, **{"from": "new", "to": str(first)})
            if first != status:
                later = min(now, decided + timedelta(hours=rng.uniform(2, 72)))
                event(later, "status", cid, None, manager, **{"from": "cust", "to": str(status)})
            if status != CandidateStatus.REVIEW and rng.random() < REPLY_SHARE:
                replied = min(now, decided + timedelta(hours=rng.uniform(0.1, 24)))
                event(replied, "reply_sent", cid, vid, recruiter, template="")
        conn.execute("COMMIT")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=5000, help="сколько кандидатов")
    ap.add_argument("--db", type=Path, help="файл базы (по умолчанию во временной папке)")
    args = ap.parse_args()
    db = args.db or Path(tempfile.mkdtemp(prefix="rvm-load-")) / f"load-{args.n}.sqlite"
    t0 = time.perf_counter()
    seed(db, args.n)
    mb = db.stat().st_size / 2**20
    print(f"{args.n} кандидатов за {time.perf_counter() - t0:.1f} с, {mb:.0f} МБ: {db}")


if __name__ == "__main__":
    sys.exit(main())
