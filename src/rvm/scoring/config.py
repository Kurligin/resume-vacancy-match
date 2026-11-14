"""Числа балла и рейтинга — все в одном месте, подбор порогов позже."""

import os

from rvm.models import Section, Status

WEIGHTS: dict[Section, float] = {
    Section.MUST: 3.0,
    Section.NICE: 1.0,
    Section.INFERRED: 1.0,
    Section.PROFSTANDARD: 0.5,
    Section.CONDITION: 0.0,
}
VALUES: dict[Status, float] = {Status.FULL: 1.0, Status.PARTIAL: 0.5, Status.NO: 0.0}
UNKNOWN_POINT = 0.25  # unknown в точечном балле; в вилке — от 0 до 1

FIT_FROM = 70.0  # балл ≥ — «подходит»
PARTIAL_FROM = 45.0  # балл ≥ — «частично», ниже — «не подходит»
REDIRECT_GAP = 10.0  # разрыв с первым местом для переадресации
CLOSE_GAP = 5.0  # соседи «почти равны», если разница меньше

WHY_NOT_FIRST = 3  # строк в «почему не первое»
REDIRECT_BEST = 3  # строк лучшей вакансии в причинах переадресации
REDIRECT_FAILS = 2  # провалов вакансии отклика в причинах переадресации

# «Разбор потока»: что требует решения рекрутера
TRIAGE_WIDE = 30.0  # вилка шире — «нужно ваше решение»
TRIAGE_NEAR = 5.0  # балл в ± от порога вердикта — «нужно ваше решение»
SPOT_EVERY = 10  # из стольких «явно не подходят» одна — на выборочную проверку

# «Сводка»: сколько дней отклик может ждать разбора
SLA_DAYS = int(os.environ.get("RVM_SLA_DAYS", "2"))
