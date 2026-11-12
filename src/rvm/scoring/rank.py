"""Рейтинги: вакансии для кандидата и кандидаты для вакансии, переадресация."""

from itertools import pairwise

from pydantic import BaseModel

from rvm.scoring import config
from rvm.scoring.score import Contribution, PairScore, Verdict


class Place(BaseModel):
    id: str
    place: int
    score: PairScore
    why_not_first: list[Contribution] = []


class Redirect(BaseModel):
    from_vacancy: str
    to_vacancy: str
    gap: float
    reasons: list[Contribution]  # сильные строки лучшей вакансии, затем провалы вакансии отклика


class CandidateRanking(BaseModel):
    places: list[Place]
    redirect: Redirect | None = None
    none_fit: bool


class VacancyRanking(BaseModel):
    places: list[Place]
    close_pairs: list[tuple[str, str]]  # соседи «почти равны» — вход для разбора близких


def _order(scores: dict[str, PairScore]) -> list[tuple[str, PairScore]]:
    """«Нет данных» последними, без блокеров выше; внутри — балл, low, id (вход не важен)."""
    return sorted(
        scores.items(),
        key=lambda kv: (
            kv[1].verdict == Verdict.NO_DATA,
            bool(kv[1].blockers),
            -kv[1].score,
            -kv[1].low,
            kv[0],
        ),
    )


def _fails(s: PairScore, n: int) -> list[Contribution]:
    """Строки, сильнее всего тянущие балл вниз; блокеры первыми."""
    bad = [c for c in s.contributions if c.loss > 0 or c.blocker]
    return sorted(bad, key=lambda c: (not c.blocker, -c.loss, c.requirement_id))[:n]


def _places(scores: dict[str, PairScore], why: bool) -> list[Place]:
    return [
        Place(
            id=k,
            place=i,
            score=s,
            why_not_first=_fails(s, config.WHY_NOT_FIRST) if why and i > 1 else [],
        )
        for i, (k, s) in enumerate(_order(scores), start=1)
    ]


def rank_vacancies(
    candidate_scores: dict[str, PairScore], applied: str | None = None
) -> CandidateRanking:
    """Вакансии одного кандидата: id вакансии → балл пары."""
    places = _places(candidate_scores, why=True)
    redirect = None
    mine = next((p for p in places if p.id == applied), None)
    best = next(
        (
            p
            for p in places
            if p is not mine
            and not p.score.blockers
            and p.score.verdict in (Verdict.FIT, Verdict.PARTIAL)
        ),
        None,
    )
    if mine and best and mine.score.verdict != Verdict.NO_DATA:
        gap = round(best.score.score - mine.score.score, 2)
        # блокер в отклике при чистой лучшей — переадресация при любом разрыве
        if mine.score.blockers or gap >= config.REDIRECT_GAP:
            strong = sorted(
                (c for c in best.score.contributions if c.gain > 0),
                key=lambda c: (-c.gain, c.requirement_id),
            )[: config.REDIRECT_BEST]
            redirect = Redirect(
                from_vacancy=mine.id,
                to_vacancy=best.id,
                gap=gap,
                reasons=strong + _fails(mine.score, config.REDIRECT_FAILS),
            )
    known = [p for p in places if p.score.verdict != Verdict.NO_DATA]
    none_fit = bool(known) and all(p.score.verdict == Verdict.NO_FIT for p in known)
    return CandidateRanking(places=places, redirect=redirect, none_fit=none_fit)


def rank_candidates(vacancy_scores: dict[str, PairScore]) -> VacancyRanking:
    """Кандидаты одной вакансии: id кандидата → балл пары."""
    places = _places(vacancy_scores, why=False)
    close = [
        (a.id, b.id)
        for a, b in pairwise(places)
        if not a.score.blockers
        and not b.score.blockers
        and Verdict.NO_DATA not in (a.score.verdict, b.score.verdict)
        and a.score.score - b.score.score < config.CLOSE_GAP
    ]
    return VacancyRanking(places=places, close_pairs=close)
