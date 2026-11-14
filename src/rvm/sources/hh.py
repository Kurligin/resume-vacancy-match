"""Источник «hh»: отклики на вакансии МФТИ через API работодателя hh.ru.

RVM_HH_TOKEN — OAuth access token менеджера работодателя (приложение на dev.hh.ru), заголовок
Authorization: Bearer; HH-User-Agent — RVM_HH_USER_AGENT, как у импорта вакансий (api.hh).
Опрашиваются открытые вакансии сервиса, импортированные с hh (source = «hh:<id на hh>»):
коллекция «Все неразобранные» GET /negotiations/response?vacancy_id=… постранично, по каждому
новому отклику — резюме по ссылке resume.actions.download.pdf.url (нет — rtf) с тем же токеном.
ref = hh:<id отклика>, повторы отсекает source_seen. За опрос — не больше POLL_ITEMS откликов
и POLL_BYTES байт, между запросами пауза PAUSE_S. 401/403/429 — опрос прекращается до
следующего раза, забранное отдаётся, причина — в статусе источника (без токена).

Только чтение: состояние откликов на hh не меняется; перевод отклика в «приглашение/отказ» на
hh — следующий этап.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from rvm.ingest import limits
from rvm.sources.base import POLL_BYTES, POLL_ITEMS, Fetched, Item

PER_PAGE = 50  # максимум hh для списка откликов
MAX_PAGES = 20
PAUSE_S = 0.5
DENIED = {
    401: "hh.ru: токен недействителен или истёк — обновите RVM_HH_TOKEN",
    403: "hh.ru: нет доступа к откликам (права менеджера или платный доступ API)",
    429: "hh.ru: превышен лимит запросов — продолжим при следующем опросе",
}


class _Stop(Exception):
    pass


def fetch(
    env: Mapping[str, str], vacancies: dict[str, dict[str, Any]], seen: Callable[[str], bool]
) -> Fetched:
    from rvm.api import hh  # клиент api.hh.ru; модуль API импортирует sources — не на уровне модуля

    token = env["RVM_HH_TOKEN"]
    items: list[Item] = []
    skipped: list[tuple[str, str]] = []
    used = 0

    def get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        time.sleep(PAUSE_S)
        try:
            return hh.get(url, params, token)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in DENIED:
                raise _Stop(DENIED[e.response.status_code]) from None
            raise

    try:
        for vid, v in vacancies.items():
            hid = v["source"].removeprefix("hh:")
            if hid == v["source"]:
                continue  # вакансия не с hh
            page, pages = 0, 1
            while page < min(pages, MAX_PAGES):
                data = get(
                    f"{hh.API}/negotiations/response",
                    {"vacancy_id": hid, "page": page, "per_page": PER_PAGE},
                ).json()
                pages, page = data.get("pages", 1), page + 1
                for n in data.get("items", []):
                    ref = f"hh:{n['id']}"
                    if seen(ref):
                        continue
                    if len(items) >= POLL_ITEMS or used >= POLL_BYTES:
                        raise _Stop("")  # остальное — в следующий опрос
                    resume = n.get("resume") or {}
                    links = (resume.get("actions") or {}).get("download") or {}
                    kind = next((k for k in ("pdf", "rtf") if (links.get(k) or {}).get("url")), "")
                    url = links[kind]["url"] if kind else ""
                    host = urlparse(url).hostname or ""
                    if host != "hh.ru" and not host.endswith(".hh.ru"):
                        skipped.append(
                            (ref, "отклик без ссылки на резюме hh.ru")
                        )  # токен — только hh
                        continue
                    try:
                        body = get(url).content
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code != 404:
                            raise
                        skipped.append((ref, "резюме удалено или скрыто соискателем"))
                        continue
                    if len(body) > limits.MAX_BYTES:
                        skipped.append((ref, f"резюме больше {limits.MAX_BYTES // 2**20} МБ"))
                        continue
                    used += len(body)
                    items.append(Item(ref, f"hh_{resume.get('id', n['id'])}.{kind}", body, vid))
    except _Stop as e:
        return Fetched(items, skipped, error=str(e))
    return Fetched(items, skipped)
