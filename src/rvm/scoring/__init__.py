"""Балл пары «резюме × вакансия» и рейтинги — только код, без модели."""

from rvm.scoring.rank import (
    CandidateRanking,
    Place,
    Redirect,
    VacancyRanking,
    rank_candidates,
    rank_vacancies,
)
from rvm.scoring.score import Contribution, PairScore, Verdict, score_pair

__all__ = [
    "CandidateRanking",
    "Contribution",
    "PairScore",
    "Place",
    "Redirect",
    "VacancyRanking",
    "Verdict",
    "rank_candidates",
    "rank_vacancies",
    "score_pair",
]
