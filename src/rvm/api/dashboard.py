"""«Сводка» (spec §2): цифры потока откликов за период — только чтение базы и журнала расхода."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from rvm.api.auth import Recruiter
from rvm.store import metrics


def router(db: Path, usage: Path) -> APIRouter:
    r = APIRouter()

    @r.get(
        "/api/dashboard",
        summary="Сводка: поток, очередь, воронка по вакансиям, качество сортировки, модель",
        description="days — период в днях по локальной дате сервера, 1 — сегодня. "
        "Определения цифр — в rvm.store.metrics.",
    )
    def dashboard(_: Recruiter, days: int = 7) -> metrics.Dashboard:
        if days not in (1, 7, 30):  # Literal[1, 7, 30] в query не принимает строку «30»
            raise HTTPException(422, "days: 1, 7 или 30")
        return metrics.dashboard(db, days, usage)

    return r
