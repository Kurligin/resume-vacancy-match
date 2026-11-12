import random

import pytest

from rvm.models import MatchRow, ReqKind, Requirement, Section, Status
from rvm.scoring import PairScore, Verdict, rank_candidates, rank_vacancies, score_pair

M, N, P, C = Section.MUST, Section.NICE, Section.PROFSTANDARD, Section.CONDITION
F, PA, NO, U = Status.FULL, Status.PARTIAL, Status.NO, Status.UNKNOWN


def _pair(*items: tuple[Section, Status]) -> tuple[list[Requirement], list[MatchRow]]:
    reqs = [
        Requirement(id=f"r{i}", text=f"требование {i}", section=s, kind=ReqKind.SKILL)
        for i, (s, _) in enumerate(items)
    ]
    rows = [MatchRow(requirement_id=f"r{i}", status=st) for i, (_, st) in enumerate(items)]
    return reqs, rows


def _score(*items: tuple[Section, Status]) -> PairScore:
    return score_pair(*_pair(*items))


def _with_score(target: float, blocker: bool = False) -> PairScore:
    """Готовый балл ровно target (вердикт пересчитывается сам)."""
    base = _score((M, NO) if blocker else (M, F))
    return base.model_copy(update={"score": target, "low": target, "high": target})


def test_formula_point_and_range() -> None:
    # веса 3+3+1+0,5 (+0 condition) = 7,5
    s = _score((M, F), (M, U), (N, PA), (P, NO), (C, NO))
    assert s.score == pytest.approx(4.25 / 7.5 * 100, abs=0.01)
    assert s.low == pytest.approx(3.5 / 7.5 * 100, abs=0.01)
    assert s.high == pytest.approx(6.5 / 7.5 * 100, abs=0.01)
    assert s.must_coverage == pytest.approx(62.5)
    assert s.blockers == []
    assert s.verdict == Verdict.PARTIAL


def test_blocker_and_verdicts() -> None:
    s = _score((M, NO), (N, F))
    assert s.blockers == ["r0"]
    assert s.verdict == Verdict.NO_FIT
    assert _score((M, F), (N, NO)).verdict == Verdict.FIT  # 75
    assert _score((M, F), (M, PA), (M, NO)).verdict == Verdict.PARTIAL  # 50


def test_missing_row_is_unknown_and_exceeds_not_in_score() -> None:
    reqs, rows = _pair((M, F), (M, F))
    rows[0].exceeds = "больше лет, чем нужно"
    s = score_pair(reqs, rows[:1])
    assert (s.low, s.high) == (50.0, 100.0)
    assert s.exceeds_count == 1
    assert score_pair(reqs, rows).score == 100.0


def test_human_edit_changes_score() -> None:
    reqs, rows = _pair((M, NO), (N, F))
    before = score_pair(reqs, rows)
    rows[0] = MatchRow(requirement_id="r0", status=F, flags=["edited_by_human"])
    after = score_pair(reqs, rows)
    assert after.score > before.score and after.blockers == []


def test_blocked_vacancy_ranks_below_even_with_higher_score() -> None:
    blocked = _score((M, NO), (N, F), (N, F), (N, F), (N, F), (N, F), (N, F), (N, F))
    clean = _score((M, PA), (N, NO))
    assert blocked.score > clean.score
    r = rank_vacancies({"a": blocked, "b": clean})
    assert [p.id for p in r.places] == ["b", "a"]
    assert r.places[1].why_not_first[0].requirement_id == "r0"  # блокер первым


def test_redirect_gap_10_fires_9_does_not() -> None:
    best, applied = _score((M, F), (N, F), (N, NO)), _score((M, F), (M, NO))
    ten = {"best": _with_score(80), "mine": _with_score(70)}
    ten["best"] = ten["best"].model_copy(update={"contributions": best.contributions})
    ten["mine"] = ten["mine"].model_copy(update={"contributions": applied.contributions})
    r = rank_vacancies(ten, applied="mine")
    assert r.redirect is not None
    assert (r.redirect.from_vacancy, r.redirect.to_vacancy, r.redirect.gap) == ("mine", "best", 10)
    assert 2 <= len(r.redirect.reasons) <= 5
    assert r.redirect.reasons[-1].requirement_id == "r1"  # провал вакансии отклика
    nine = {"best": _with_score(79), "mine": _with_score(70)}
    assert rank_vacancies(nine, applied="mine").redirect is None
    assert rank_vacancies(ten, applied="best").redirect is None
    assert rank_vacancies(ten).redirect is None


def test_none_fit() -> None:
    low = {"a": _with_score(30), "b": _with_score(44)}
    assert rank_vacancies(low).none_fit
    low["b"] = _score((M, F))
    assert not rank_vacancies(low).none_fit


def test_permutation_does_not_change_ranking() -> None:
    scores = {
        f"v{i}": _with_score(s, b)
        for i, (s, b) in enumerate(
            [(50, False), (50, False), (90, True), (70, False), (50, False), (10, False)]
        )
    }
    expected = [p.id for p in rank_vacancies(scores).places]
    assert expected[:2] == ["v3", "v0"] and expected[-1] == "v2"
    items = list(scores.items())
    for seed in range(5):
        random.Random(seed).shuffle(items)
        assert [p.id for p in rank_vacancies(dict(items)).places] == expected
        assert [p.id for p in rank_candidates(dict(items)).places] == expected


def test_close_pairs_diff_4_marked_5_not() -> None:
    r = rank_candidates(
        {
            "a": _with_score(80),
            "b": _with_score(76),
            "c": _with_score(71),
            "d": _with_score(70, True),
        }
    )
    assert [p.id for p in r.places] == ["a", "b", "c", "d"]
    assert [p.place for p in r.places] == [1, 2, 3, 4]
    assert r.close_pairs == [("a", "b")]  # b–c разница 5, c–d с блокером


def test_blocker_caps_verdict_at_partial() -> None:
    assert _with_score(80, blocker=True).verdict == Verdict.PARTIAL
    assert _with_score(30, blocker=True).verdict == Verdict.NO_FIT


def test_no_weight_is_no_data_and_skipped_in_none_fit() -> None:
    for s in (_score(), _score((C, NO), (C, F))):
        assert s.verdict == Verdict.NO_DATA
    r = rank_vacancies({"empty": _score(), "low": _with_score(30), "low2": _with_score(20)})
    assert r.none_fit
    assert r.places[-1].id == "empty" and r.places[-1].score.verdict == Verdict.NO_DATA
    assert not rank_vacancies({"empty": _score()}).none_fit
    assert rank_candidates({"a": _score(), "b": _score((C, F))}).close_pairs == []


def test_redirect_never_to_no_fit_or_blocked() -> None:
    r = rank_vacancies({"best": _with_score(40), "mine": _with_score(20)}, applied="mine")
    assert r.redirect is None  # лучшая «не подходит»
    blocked_best = {"best": _with_score(90, True), "mine": _with_score(50)}
    assert rank_vacancies(blocked_best, applied="mine").redirect is None
    no_data = {"empty": _score(), "mine": _with_score(20)}
    assert rank_vacancies(no_data, applied="mine").redirect is None
    assert rank_vacancies({"a": _with_score(80), "mine": _score()}, applied="mine").redirect is None


def test_redirect_from_blocked_to_clean_regardless_of_gap() -> None:
    scores = {"clean": _with_score(60), "mine": _with_score(80, True)}
    r = rank_vacancies(scores, applied="mine")
    assert r.redirect is not None
    assert (r.redirect.from_vacancy, r.redirect.to_vacancy) == ("mine", "clean")
    assert r.redirect.reasons[-1].blocker
    # чистая лучшая перескакивает «не подходит» и заблокированные выше по баллу
    scores |= {"bad": _with_score(30), "blocked": _with_score(95, True)}
    assert rank_vacancies(scores, applied="mine").redirect.to_vacancy == "clean"  # type: ignore[union-attr]
