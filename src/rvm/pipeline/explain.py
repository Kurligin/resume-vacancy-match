"""Выводы по паре: модель пишет сильные стороны, риски и рекомендацию только по готовой таблице,
код отбрасывает пункты без ссылок и с фактами вне таблицы; иначе — запасной вариант из таблицы."""

import re
from importlib.resources import files

import openai
from pydantic import BaseModel

from rvm.facts import Discrepancy, ResumeFacts
from rvm.llm import LLMClient, LLMError
from rvm.models import MatchRow, Requirement, Section, Status
from rvm.refs.skills import hints, mentioned
from rvm.scoring import PairScore, Verdict

PROMPT_VERSION = "explain.v1"
PROMPT = files("rvm") / "prompts" / f"{PROMPT_VERSION}.md"
MODEL_ERRORS = (LLMError, openai.OpenAIError)
VERDICT_ID = "VERDICT"
SNAPSHOT_ID = "SNAPSHOT"
MAX_POINTS = 6
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")
STATUS_RU = {
    Status.FULL: "выполнено",
    Status.PARTIAL: "частично",
    Status.NO: "не выполнено",
    Status.UNKNOWN: "нет данных",
}


class Check(BaseModel):
    """Несостыковка в резюме: id для ссылок, текст для таблицы, вопрос-шаблон кандидату."""

    id: str
    text: str
    ask: str


class Point(BaseModel):
    text: str
    refs: list[str]  # id требований (R…) или проверок (D…, SNAPSHOT, VERDICT)


class Insights(BaseModel):
    strengths: list[Point]
    risks: list[Point]
    recommendation: Point
    dropped: int = 0  # пунктов модели отброшено проверкой
    fallback: bool = False  # хоть один раздел собран из таблицы без модели


class _Explain(BaseModel):
    strengths: list[Point]
    risks: list[Point]
    recommendation: Point | None


def build_checks(
    facts: ResumeFacts, discrepancies: list[Discrepancy], snapshot: str | None
) -> list[Check]:
    checks = []
    for i, d in enumerate(discrepancies, 1):
        p = facts.periods[d.period] if d.period is not None else None
        where = f"место работы ({p.dates_raw})" if p else "общий стаж"
        claim = (p.declared_raw if p else facts.declared_total_raw) or ""
        text = f"{where}: заявлено {claim} {d.declared} мес., по датам {d.computed} мес."
        text = re.sub(r"\s+", " ", text)
        ask = (
            f"Уточните, пожалуйста, {where}: в резюме указано {claim or str(d.declared) + ' мес.'}"
        )
        checks.append(Check(id=f"D{i}", text=text, ask=f"{ask}, а по датам выходит иначе?"))
    if snapshot:
        checks.append(
            Check(
                id=SNAPSHOT_ID,
                text=snapshot,
                ask=f"Актуально ли резюме? По датам {snapshot.removeprefix('резюме, ')}.",
            )
        )
    return checks


def _row_text(req: Requirement, row: MatchRow | None) -> str:
    parts = [req.text]
    if row:
        parts += [e.quote for e in row.evidence] + [row.note, row.exceeds or ""]
    return " ".join(p for p in parts if p)


def table(
    reqs: list[Requirement], rows: list[MatchRow], score: PairScore, checks: list[Check]
) -> dict[str, str]:
    """id → текст строки таблицы (требование, цитаты, пояснение), включая проверки и вердикт."""
    by_id = {r.requirement_id: r for r in rows}
    out = {r.id: _row_text(r, by_id.get(r.id)) for r in reqs}
    out.update({c.id: c.text for c in checks})
    out[VERDICT_ID] = (
        f"вердикт {score.verdict}, балл {score.score}, вилка {score.low}–{score.high}, "
        f"покрытие обязательных {score.must_coverage}"
    )
    return out


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUM.findall(text)}


def _grounded(point: Point, rows: dict[str, str], facts_text: str, facts_nums: set[float]) -> bool:
    """Ссылки валидны, навыки и числа пункта есть в его строках или в фактах."""
    if not point.refs or any(r not in rows for r in point.refs):
        return False
    allowed = " ".join(rows[r] for r in point.refs) + " " + facts_text
    return supported(point.text, allowed, facts_nums)


def supported(text: str, allowed: str, nums: set[float] | None = None) -> bool:
    """Навыки и числа текста есть в allowed (или среди nums): модель ничего не выдумала."""
    terms = set(hints(text)) | set(_LATIN.findall(text))
    if any(not mentioned(t, allowed) for t in terms):
        return False
    return _numbers(text) <= _numbers(allowed) | (nums or set())


def fallback(
    reqs: list[Requirement],
    rows: list[MatchRow],
    score: PairScore,
    checks: list[Check],
    redirect_to: str | None,
) -> Insights:
    """Выводы из таблицы без модели."""
    req_of = {r.id: r for r in reqs}
    gain = {c.requirement_id: c.gain for c in score.contributions}
    must = [
        r
        for r in rows
        if r.requirement_id in req_of and req_of[r.requirement_id].section == Section.MUST
    ]
    strengths = [
        Point(
            text=req_of[r.requirement_id].text
            + (f": «{r.evidence[0].quote}»" if r.evidence else ""),
            refs=[r.requirement_id],
        )
        for r in sorted(must, key=lambda r: -gain.get(r.requirement_id, 0))
        if r.status == Status.FULL
    ][:3]
    risks = [
        Point(
            text=f"{STATUS_RU[r.status].capitalize()}: {req_of[r.requirement_id].text}",
            refs=[r.requirement_id],
        )
        for status in (Status.NO, Status.PARTIAL, Status.UNKNOWN)
        for r in must
        if r.status == status
    ]
    risks += [Point(text=c.text[0].upper() + c.text[1:], refs=[c.id]) for c in checks]
    return Insights(
        strengths=strengths,
        risks=risks[:MAX_POINTS],
        recommendation=recommend(score, redirect_to),
        fallback=True,
    )


def recommend(score: PairScore, redirect_to: str | None) -> Point:
    """Рекомендация рекрутеру по вердикту и переадресации."""
    if redirect_to:
        text = f"Предложить вакансию «{redirect_to}»: по ней соответствие выше."
    elif score.verdict == Verdict.FIT:
        text = f"Пригласить на собеседование: балл {round(score.score)}."
    elif score.verdict == Verdict.PARTIAL:
        text = (
            f"Уточнить пробелы на собеседовании или оставить в резерве: балл {round(score.score)}."
        )
    elif score.verdict == Verdict.NO_FIT:
        text = f"Отказать или оставить в резерве под другие вакансии: балл {round(score.score)}."
    else:
        text = "Недостаточно данных для вывода: в вакансии не найдены требования."
    return Point(text=text, refs=[VERDICT_ID])


def _prompt_table(reqs: list[Requirement], rows: list[MatchRow], checks: list[Check]) -> str:
    by_id = {r.requirement_id: r for r in rows}
    lines = []
    for r in reqs:
        row = by_id.get(r.id)
        status = STATUS_RU[row.status] if row else STATUS_RU[Status.UNKNOWN]
        quotes = "; ".join(f"«{e.quote}» (L{e.line})" for e in row.evidence) if row else ""
        extra = f"; сверх требования: {row.exceeds}" if row and row.exceeds else ""
        flags = f"; флаги: {', '.join(row.flags)}" if row and row.flags else ""
        lines.append(f"{r.id} [{r.section}] {r.text} — {status}; цитаты: {quotes}{extra}{flags}")
    lines += [f"{c.id} [проверка] {c.text}" for c in checks]
    return "\n".join(lines)


def explain(
    reqs: list[Requirement],
    rows: list[MatchRow],
    score: PairScore,
    checks: list[Check],
    total_months: int,
    redirect_to: str | None,
    llm: LLMClient,
) -> Insights:
    base = fallback(reqs, rows, score, checks, redirect_to)
    if not rows:
        return base
    rows_text = table(reqs, rows, score, checks)
    if redirect_to:
        rows_text[VERDICT_ID] += f"; переадресация: лучше подходит вакансия «{redirect_to}»"
    facts_text = f"стаж {total_months} мес. {rows_text[VERDICT_ID]}"
    facts_nums = _numbers(facts_text) | {total_months // 12, round(total_months / 12, 1)}
    facts_nums |= {float(round(x)) for x in (score.score, score.low, score.high)}
    user = (
        _prompt_table(reqs, rows, checks)
        + f"\n{VERDICT_ID} [итог] {rows_text[VERDICT_ID]}; стаж по датам {total_months} мес."
    )
    messages = [
        {"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": user},
    ]
    try:
        answer: _Explain = llm.complete(messages, _Explain, PROMPT_VERSION)
    except MODEL_ERRORS:
        return base

    def keep(points: list[Point]) -> list[Point]:
        return [p for p in points if _grounded(p, rows_text, facts_text, facts_nums)]

    strengths, risks = keep(answer.strengths), keep(answer.risks)
    rec = keep([answer.recommendation] if answer.recommendation else [])
    total = len(answer.strengths) + len(answer.risks) + (1 if answer.recommendation else 0)
    return Insights(
        strengths=strengths[:MAX_POINTS] or base.strengths,
        risks=risks[:MAX_POINTS] or base.risks,
        recommendation=rec[0] if rec else base.recommendation,
        dropped=total - len(strengths) - len(risks) - len(rec),
        fallback=not (strengths and risks and rec),
    )
