"""Балл пары: Σ(вес × значение) / Σ(вес) × 100, вилка по unknown, блокеры, вердикт."""

from enum import StrEnum

from pydantic import BaseModel, computed_field

from rvm.models import MatchRow, Requirement, Section, Status
from rvm.scoring import config


class Verdict(StrEnum):
    FIT = "fit"  # подходит
    PARTIAL = "partial"  # частично
    NO_FIT = "no_fit"  # не подходит


class Contribution(BaseModel):
    """Вклад строки: gain — сколько баллов дала, loss — сколько недодала (в точке)."""

    requirement_id: str
    section: Section
    status: Status
    weight: float
    gain: float
    loss: float
    blocker: bool


class PairScore(BaseModel):
    score: float
    low: float
    high: float
    must_coverage: float  # то же, что балл, но только по must; 100, если must нет
    blockers: list[str]
    exceeds_count: int  # «сверх требования», в балл не входит
    contributions: list[Contribution]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> Verdict:
        if self.score >= config.FIT_FROM:
            return Verdict.FIT
        return Verdict.PARTIAL if self.score >= config.PARTIAL_FROM else Verdict.NO_FIT


def _value(status: Status, unknown: float) -> float:
    return unknown if status == Status.UNKNOWN else config.VALUES[status]


def _pct(items: list[tuple[float, Status]], unknown: float, empty: float) -> float:
    total = sum(w for w, _ in items)
    if not total:
        return empty
    return round(sum(w * _value(s, unknown) for w, s in items) / total * 100, 2)


def score_pair(requirements: list[Requirement], rows: list[MatchRow]) -> PairScore:
    """Строки без пары в rows считаются unknown; правка рекрутера — обычная строка."""
    by_id = {r.requirement_id: r for r in rows}
    items = [
        (r, by_id[r.id].status if r.id in by_id else Status.UNKNOWN, config.WEIGHTS[r.section])
        for r in requirements
    ]
    weighted = [(w, s) for _, s, w in items]
    total = sum(w for w, _ in weighted)
    contributions = [
        Contribution(
            requirement_id=r.id,
            section=r.section,
            status=s,
            weight=w,
            gain=round(w * _value(s, config.UNKNOWN_POINT) / total * 100, 2) if total else 0.0,
            loss=(
                round(w * (1 - _value(s, config.UNKNOWN_POINT)) / total * 100, 2) if total else 0.0
            ),
            blocker=r.section == Section.MUST and s == Status.NO,
        )
        for r, s, w in items
    ]
    return PairScore(
        score=_pct(weighted, config.UNKNOWN_POINT, 0.0),
        low=_pct(weighted, 0.0, 0.0),
        high=_pct(weighted, 1.0, 0.0),
        must_coverage=_pct(
            [(1.0, s) for r, s, _ in items if r.section == Section.MUST],
            config.UNKNOWN_POINT,
            100.0,
        ),
        blockers=[c.requirement_id for c in contributions if c.blocker],
        exceeds_count=sum(1 for r in requirements if r.id in by_id and by_id[r.id].exceeds),
        contributions=contributions,
    )
