"""API «Входящие»: состояние источников откликов и «Проверить сейчас»."""

import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from rvm.api.auth import Recruiter
from rvm.sources import Inbox


class SourceEntry(BaseModel):
    at: str
    source: str
    name: str  # файлы пачки через запятую; пусто — пропуск или кандидат удалён
    vacancy: str | None  # id вакансии отклика
    batch_id: str | None  # пачка в /api/uploads/{id}
    candidate_id: int | None  # первый кандидат пачки, когда разобран
    state: str  # queued / reading / done / error / skipped
    message: str


class SourceState(BaseModel):
    name: str
    title: str
    account: str  # адрес ящика для показа (без пароля); пусто — нет
    every_minutes: float  # период фонового опроса; 0 — опрос не запущен
    configured: bool
    needs: list[str]  # какие переменные окружения задать, если не настроен
    running: bool
    last_check: str | None
    last_error: str
    today_items: int
    today_skipped: int
    recent: list[SourceEntry]  # последние 20, новые сверху


class CheckStarted(BaseModel):
    started: bool  # False — опрос этого источника уже идёт


def router(inbox: Inbox) -> APIRouter:
    r = APIRouter()

    @r.get("/api/sources", summary="Источники откликов: почта, папка")
    def list_sources(_: Recruiter) -> list[SourceState]:
        return [SourceState(**s) for s in inbox.view()]

    @r.post(
        "/api/sources/{name}/check",
        status_code=202,
        summary="Проверить источник сейчас",
        description="Опрос идёт в фоне; итог — в GET /api/sources (running, last_check, recent).",
    )
    def check(name: str, _: Recruiter) -> CheckStarted:
        if name not in inbox.sources:
            raise HTTPException(404, f"нет источника {name}")
        if missing := inbox.missing(name):
            raise HTTPException(409, f"источник не настроен: задайте {', '.join(missing)}")
        if inbox.running(name):
            return CheckStarted(started=False)
        threading.Thread(target=inbox.check, args=(name,), daemon=True).start()
        return CheckStarted(started=True)

    return r
