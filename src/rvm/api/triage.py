"""«Разбор потока» (spec §6б П1–П2) и счётчики базы: корзины, решения пачкой, выборочная
проверка, «ответ отправлен». Система только предлагает — статус меняет рекрутер."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from rvm import store
from rvm.api.auth import Recruiter
from rvm.pipeline.analyze import ReportStatus, VacancyResult
from rvm.pipeline.explain import Point
from rvm.scoring import Verdict, config
from rvm.store import CandidateStatus

Bucket = Literal["need", "fit", "nofit"]
DECISIONS: dict[int, CandidateStatus] = {
    1: CandidateStatus.CUST,  # заказчику
    2: CandidateStatus.ALT,  # предложить другую вакансию
    3: CandidateStatus.RESERVE,
    4: CandidateStatus.REJECTED,
}
_PROPOSE = {Verdict.FIT: 1, Verdict.PARTIAL: 3, Verdict.NO_DATA: 3, Verdict.NO_FIT: 4}
_STATUS = {"full": "есть", "partial": "частично", "no": "нет", "unknown": "не указано"}


class Reason(BaseModel):
    text: str
    lines: list[int]  # строки резюме — цитаты по требованиям из ссылок


class TriageItem(BaseModel):
    id: int
    bucket: Bucket
    why: list[str]  # почему в этой корзине
    position: str | None
    vacancy_id: str
    title: str
    score: float
    low: float
    high: float
    verdict: Verdict
    pros: list[Reason]
    cons: list[Reason]
    proposed: int  # 1 заказчику, 2 другая вакансия, 3 резерв, 4 отказ
    spot_check: bool = False  # выборочная проверка: показать отдельно «согласны?»


class Triage(BaseModel):
    need: list[TriageItem]
    fit: list[TriageItem]
    nofit: list[TriageItem]
    done: int  # уже с решением
    pending: int  # оценка ещё не готова
    check_file: int = 0  # обезличивание не прошло: нужна проверка файла
    totals: dict[Bucket, int] = {}  # сколько в корзине всего (при limit в списке — первые)


class DecideBody(BaseModel):
    candidate_ids: list[int] = Field(min_length=1)
    decision: Literal[1, 2, 3, 4]
    vacancy_id: str | None = None
    comment: str = ""


class SpotBody(BaseModel):
    candidate_id: int
    agree: bool
    comment: str = ""


class ReplyBody(BaseModel):
    template: str = ""
    vacancy_id: str | None = None


def _lines(v: VacancyResult, refs: list[str]) -> list[int]:
    return sorted({e.line for r in v.rows if r.requirement_id in refs for e in r.evidence})


def _reasons(v: VacancyResult, points: list[Point] | None, good: bool) -> list[Reason]:
    """3 «за» или «против»: из выводов, без них — из таблицы (самые весомые строки)."""
    if points:
        return [Reason(text=p.text, lines=_lines(v, p.refs)) for p in points[:3]]
    text = {r.id: r.text for r in v.requirements}
    key = (lambda c: -c.gain) if good else (lambda c: -c.loss)
    picked = [c for c in sorted(v.score.contributions, key=key) if (c.gain if good else c.loss)]
    return [
        Reason(
            text=f"{text.get(c.requirement_id, c.requirement_id)}: {_STATUS[c.status]}",
            lines=_lines(v, [c.requirement_id]),
        )
        for c in picked[:3]
    ]


def classify(b: store.Brief, disagreed: bool = False) -> tuple[Bucket, list[str], int]:
    """Корзина, причины и предлагаемое решение по лучшей вакансии кандидата (b.vacancy_id есть)."""
    assert b.score is not None and b.low is not None and b.high is not None
    verdict = Verdict(b.verdict)
    need = []
    if disagreed:
        need.append("вы не согласились с «не подходит» на выборочной проверке")
    if b.high - b.low > config.TRIAGE_WIDE:
        need.append(f"широкая вилка {b.low:g}–{b.high:g}")
    if any(abs(b.score - t) <= config.TRIAGE_NEAR for t in (config.FIT_FROM, config.PARTIAL_FROM)):
        need.append(f"балл {b.score:g} у порога вердикта")
    if b.redirect_from:
        src = b.redirect_title or b.redirect_from
        need.append(f"откликнулся на «{src}», лучше подходит «{b.title}»")
    if any(f"D{i}" not in b.clarified for i in range(1, b.discrepancies + 1)):
        need.append("несостыковки в стаже")
    if need:
        return "need", need, 2 if b.redirect_from else _PROPOSE[verdict]
    if verdict == Verdict.FIT and not b.blockers:
        return "fit", [f"подходит: балл {b.score:g}, блокеров нет"], 1
    if verdict == Verdict.NO_FIT or b.blockers:
        why = [f"не выполнено обязательное: {t}" for t in b.blockers]
        return "nofit", why or [f"не подходит: балл {b.score:g}"], 4
    return "need", [f"частичное совпадение, балл {b.score:g}"], _PROPOSE[verdict]


def router(db: Path, batch_ids: Callable[[str], list[int] | None]) -> APIRouter:
    """batch_ids — кандидаты пачки загрузки (None — нет такой пачки)."""
    r = APIRouter()

    @r.get(
        "/api/triage",
        summary="Разбор потока: корзины кандидатов без решения",
        description="need — нужно решение (широкая вилка, балл у порога, переадресация, "
        "несостыковки), fit — явно подходят, nofit — явно не подходят; в nofit каждый "
        f"{config.SPOT_EVERY}-й помечен spot_check. batch — только кандидаты пачки загрузки. "
        "limit — не больше стольких в каждой корзине, сколько всего — в totals.",
    )
    def triage(
        _: Recruiter,
        batch: str | None = None,
        limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    ) -> Triage:
        ids = None
        if batch is not None:
            ids = batch_ids(batch)
            if ids is None:
                raise HTTPException(404, "нет такой пачки")
        info = store.summaries(db, ids)
        spots = store.spot_answers(db)
        undecided = [c["resume_id"] for c in info.values() if c["status"] in store.UNDECIDED]
        # корзины — по колонкам и json_extract; полный отчёт — только для попавших в ответ
        briefs = store.briefs(db, undecided)
        out = Triage(need=[], fit=[], nofit=[], done=0, pending=0)
        found: dict[Bucket, list[tuple[int, list[str], int]]] = {"need": [], "fit": [], "nofit": []}
        for cid, c in sorted(info.items()):
            if c["status"] not in store.UNDECIDED:
                out.done += 1
                continue
            b = briefs.get(c["resume_id"])
            if b is not None and b.status == ReportStatus.PRIVACY_ERROR:
                out.check_file += 1
                continue
            if b is None or b.vacancy_id is None:
                out.pending += 1
                continue
            bucket, why, proposed = classify(b, spots.get(cid) is False)
            found[bucket].append((cid, why, proposed))
        # проверенные (и перенесённые несогласием в need) остаются в счёте — метка не кочует
        counted = sorted(
            [x[0] for x in found["nofit"]] + [x[0] for x in found["need"] if x[0] in spots]
        )
        marked = {cid for i, cid in enumerate(counted) if i % config.SPOT_EVERY == 0}
        shown = {bk: rows[:limit] for bk, rows in found.items()}
        reports = store.load_reports(
            db, [info[x[0]]["resume_id"] for rows in shown.values() for x in rows], brief=True
        )
        for bucket, rows in shown.items():
            out.totals[bucket] = len(found[bucket])
            for cid, why, proposed in rows:
                v = reports[info[cid]["resume_id"]].vacancies[0]
                getattr(out, bucket).append(
                    TriageItem(
                        id=cid,
                        bucket=bucket,
                        why=why,
                        position=info[cid]["position"],
                        vacancy_id=v.id,
                        title=v.title,
                        score=v.score.score,
                        low=v.score.low,
                        high=v.score.high,
                        verdict=v.verdict,
                        pros=_reasons(v, v.insights and v.insights.strengths, True),
                        cons=_reasons(v, v.insights and v.insights.risks, False),
                        proposed=proposed,
                        spot_check=bucket == "nofit" and cid in marked and cid not in spots,
                    )
                )
        return out

    @r.post("/api/triage/decide", summary="Решение по одному кандидату или пачкой")
    def decide(body: DecideBody, user: Recruiter) -> dict[str, bool]:
        try:
            store.set_statuses(
                db,
                body.candidate_ids,
                DECISIONS[body.decision],
                who=user.who,
                vacancy_id=body.vacancy_id,
                decision=body.decision,
                comment=body.comment,
            )
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        return {"ok": True}

    @r.post(
        "/api/triage/spot",
        summary="Выборочная проверка: согласны ли с «не подходит»",
        description="Ответ пишется в журнал; при несогласии кандидат уходит в «нужно решение».",
    )
    def spot(body: SpotBody, user: Recruiter) -> dict[str, bool]:
        try:
            store.log(
                db,
                "spot_check",
                body.candidate_id,
                who=user.who,
                agree=body.agree,
                comment=body.comment,
            )
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        return {"ok": True}

    @r.post("/api/candidates/{candidate_id}/reply-sent", summary="Ответ кандидату отправлен")
    def reply_sent(candidate_id: int, body: ReplyBody, user: Recruiter) -> dict[str, bool]:
        try:
            store.log(
                db,
                "reply_sent",
                candidate_id,
                who=user.who,
                vacancy_id=body.vacancy_id,
                template=body.template,
            )
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        return {"ok": True}

    @r.get("/api/stats", summary="Счётчики шапки базы: по статусам, ждут ответа, время до ответа")
    def stats(_: Recruiter) -> dict[str, Any]:
        return store.stats(db)

    return r
