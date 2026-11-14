"""«Сводка» (spec §2): поток откликов, очередь, воронка по вакансиям, «не потеряли», качество
сортировки, модель. Только чтение базы и usage.jsonl, без вызовов модели.

Определения:
- период — days календарных дней по локальной дате сервера, включая сегодня (1 — сегодня);
- отклик — карточка кандидата, созданная в период (candidates.created_at); источник —
  candidates.source; вакансия отклика — analysis.applied последней версии резюме (может быть
  угадана эвристикой; нет разбора — vacancy_id null). Удалённые карточки не считаются,
  склеенная считается по дате оставшейся;
- разобрано — у кандидата было событие status (рекрутер принял решение);
- ждут разбора — сейчас статус new/review; возраст — дни по локальной дате от отклика; дольше
  SLA — возраст больше RVM_SLA_DAYS. Срез на сейчас, не за период;
- передано заказчику — был переход в cust; решение заказчика — переход из cust в другой статус
  (кто бы ни поставил); ждут заказчика — сейчас cust, возраст — от последнего перехода в cust
  (срез на сейчас, в воронке тоже);
- не потеряли — переходы статуса в alt / reserve за период, разные кандидаты;
- воронка — кандидаты с откликом в период; отказ (rejected, cust_rejected) и резерв/другая
  (reserve, alt) — по текущему статусу; медианы в днях по этим кандидатам: отклик → первое
  решение рекрутера, первый переход в cust → первое решение заказчика;
- выборочная проверка — последний ответ spot_check по кандидату за период: agreed из checked;
  поправлено строк — разные строки (кандидат, вакансия, требование) с событием answer за период;
- модель — резюме, загруженные в период: resumes — разбор ok, errors — разбор со статусом не ok
  (обезличивание не прошло), unfinished — разбора в базе нет; avg_seconds — среднее elapsed_s
  удачных разборов (одно резюме по всем открытым вакансиям); токены — записи usage.jsonl с
  меткой at за период; cost_usd всегда null — цен моделей в коде нет.

Ограничения: ошибки чтения файла и исключения разбора хранятся только в памяти пачек и в базу не
попадают — такие резюме видны лишь как unfinished (вместе с разбором, который ещё идёт), а
нечитаемые файлы не видны совсем; записи usage.jsonl без метки at (до B4) в расход не входят."""

import json
import sqlite3
import statistics
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from pydantic import BaseModel

from rvm.scoring import config
from rvm.store.db import CandidateStatus, _tx
from rvm.store.flow import UNDECIDED

C = CandidateStatus
KEPT = (C.ALT, C.RESERVE)
REJECTED = (C.REJECTED, C.CUST_REJECTED)


class Day(BaseModel):
    date: date
    total: int
    by_source: dict[str, int]


class Waiting(BaseModel):
    count: int
    oldest_days: int | None


class Queue(Waiting):
    over_sla: int
    sla_days: int


class Kept(BaseModel):
    alt: int
    reserve: int
    total: int  # разные кандидаты


class Quality(BaseModel):
    spot_checked: int
    spot_agreed: int
    rows_fixed: int


class VacancyFunnel(BaseModel):
    vacancy_id: str | None
    title: str | None
    applied: int
    reviewed: int
    to_customer: int
    customer_decided: int
    rejected: int
    kept: int
    median_decision_days: float | None
    median_customer_days: float | None
    customer_waiting: int
    customer_oldest_days: int | None


class ModelUsage(BaseModel):
    resumes: int
    avg_seconds: float | None
    errors: int
    unfinished: int
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None


class Dashboard(BaseModel):
    days: int
    since: date
    today: date
    applications: int
    by_source: dict[str, int]
    flow: list[Day]
    queue: Queue
    customer: Waiting
    kept: Kept
    quality: Quality
    vacancies: list[VacancyFunnel]
    model: ModelUsage


def _span(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 1) if xs else None


def _count(keys: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in keys:
        out[k] = out.get(k, 0) + 1
    return out


def _usage(path: Path | None, start: str) -> tuple[int, int, int]:
    calls = prompt = completion = 0
    if path is None or not path.exists():
        return calls, prompt, completion
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue  # недописанная строка при падении процесса
        if (r.get("at") or "") >= start:
            calls += 1
            prompt += r.get("prompt_tokens") or 0
            completion += r.get("completion_tokens") or 0
    return calls, prompt, completion


def dashboard(db: Path, days: int, usage: Path | None, now: datetime | None = None) -> Dashboard:
    """Цифры «Сводки» за days дней; определения — в docstring модуля."""
    now = now or datetime.now().astimezone()  # локальное время сервера
    tz, today = now.tzinfo, now.date()
    since = today - timedelta(days=days - 1)
    # в базе UTC в isoformat: строки сравниваются в хронологическом порядке
    start = datetime.combine(since, time(), tz).astimezone(UTC).isoformat()

    def age(at: str) -> int:
        return (today - datetime.fromisoformat(at).astimezone(tz).date()).days

    with _tx(db, write=False) as conn:
        cands = conn.execute(
            "SELECT c.id, c.status, c.created_at, c.source, (SELECT json_extract(r.analysis,"
            " '$.applied') FROM resumes r WHERE r.candidate_id=c.id ORDER BY r.id DESC LIMIT 1)"
            " AS applied FROM candidates c ORDER BY c.id"
        ).fetchall()
        moves = conn.execute(
            "SELECT candidate_id AS cid, at, json_extract(details, '$.from') AS src,"
            " json_extract(details, '$.to') AS dst FROM events WHERE type='status' ORDER BY id"
        ).fetchall()
        spots = conn.execute(
            "SELECT candidate_id, json_extract(details, '$.agree') FROM events"
            " WHERE type='spot_check' AND at>=? ORDER BY id",
            (start,),
        ).fetchall()
        fixed = conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT candidate_id, vacancy_id,"
            " json_extract(details, '$.requirement') FROM events WHERE type='answer' AND at>=?)",
            (start,),
        ).fetchone()[0]
        parsed = conn.execute(
            "SELECT json_extract(analysis, '$.status') AS status,"
            " json_extract(analysis, '$.elapsed_s') AS elapsed FROM resumes WHERE created_at>=?",
            (start,),
        ).fetchall()
        titles = {str(r[0]): r[1] for r in conn.execute("SELECT id, title FROM vacancies")}

    first: dict[int, str] = {}  # первое решение рекрутера
    cust_first: dict[int, str] = {}
    cust_last: dict[int, str] = {}
    cust_out: dict[int, str] = {}  # первое решение заказчика
    kept: dict[str, set[int]] = {s: set() for s in KEPT}
    for m in moves:
        cid = m["cid"]
        first.setdefault(cid, m["at"])
        if m["dst"] == C.CUST and m["src"] != C.CUST:
            cust_first.setdefault(cid, m["at"])
            cust_last[cid] = m["at"]
        if m["src"] == C.CUST and m["dst"] != C.CUST:
            cust_out.setdefault(cid, m["at"])
        if m["dst"] in KEPT and m["at"] >= start:
            kept[m["dst"]].add(cid)

    cohort = [c for c in cands if c["created_at"] >= start]
    local = {c["id"]: datetime.fromisoformat(c["created_at"]).astimezone(tz).date() for c in cohort}
    flow = [
        Day(
            date=d,
            total=sum(1 for c in cohort if local[c["id"]] == d),
            by_source=_count([c["source"] for c in cohort if local[c["id"]] == d]),
        )
        for d in (since + timedelta(days=i) for i in range(days))
    ]

    waiting = [age(c["created_at"]) for c in cands if c["status"] in UNDECIDED]
    at_cust = [c for c in cands if c["status"] == C.CUST]

    def cust_age(c: sqlite3.Row) -> int:
        return age(cust_last.get(c["id"], c["created_at"]))

    rows = []
    for vid in sorted({c["applied"] for c in cohort + at_cust}, key=lambda v: (v is None, v)):
        mine = [c for c in cohort if c["applied"] == vid]
        ids = [c["id"] for c in mine]
        wait = [cust_age(c) for c in at_cust if c["applied"] == vid]
        rows.append(
            VacancyFunnel(
                vacancy_id=vid,
                title=titles.get(vid) if vid else None,
                applied=len(mine),
                reviewed=sum(1 for i in ids if i in first),
                to_customer=sum(1 for i in ids if i in cust_first),
                customer_decided=sum(1 for i in ids if i in cust_out),
                rejected=sum(1 for c in mine if c["status"] in REJECTED),
                kept=sum(1 for c in mine if c["status"] in KEPT),
                median_decision_days=_median(
                    [_span(c["created_at"], first[c["id"]]) for c in mine if c["id"] in first]
                ),
                median_customer_days=_median(
                    [_span(cust_first[i], cust_out[i]) for i in ids if i in cust_out]
                ),
                customer_waiting=len(wait),
                customer_oldest_days=max(wait, default=None),
            )
        )
    rows.sort(key=lambda r: -r.applied)  # устойчиво: при равенстве — по id вакансии

    last_spot = {cid: bool(agree) for cid, agree in spots}
    ok = [r["elapsed"] for r in parsed if r["status"] == "ok"]
    calls, prompt, completion = _usage(usage, start)
    cust_ages = [cust_age(c) for c in at_cust]
    return Dashboard(
        days=days,
        since=since,
        today=today,
        applications=len(cohort),
        by_source=_count([c["source"] for c in cohort]),
        flow=flow,
        queue=Queue(
            count=len(waiting),
            oldest_days=max(waiting, default=None),
            over_sla=sum(1 for a in waiting if a > config.SLA_DAYS),
            sla_days=config.SLA_DAYS,
        ),
        customer=Waiting(count=len(at_cust), oldest_days=max(cust_ages, default=None)),
        kept=Kept(
            alt=len(kept[C.ALT]),
            reserve=len(kept[C.RESERVE]),
            total=len(kept[C.ALT] | kept[C.RESERVE]),
        ),
        quality=Quality(
            spot_checked=len(last_spot),
            spot_agreed=sum(last_spot.values()),
            rows_fixed=fixed,
        ),
        vacancies=rows,
        model=ModelUsage(
            resumes=len(ok),
            avg_seconds=round(statistics.mean(ok), 1) if ok else None,
            errors=sum(1 for r in parsed if r["status"] not in (None, "ok")),
            unfinished=sum(1 for r in parsed if r["status"] is None),
            calls=calls,
            prompt_tokens=prompt,
            completion_tokens=completion,
        ),
    )
