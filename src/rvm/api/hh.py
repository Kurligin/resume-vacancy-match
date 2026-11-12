"""Открытые вакансии МФТИ с публичного api.hh.ru: список с кэшем на сутки и импорт выбранных."""

import html
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from rvm import store
from rvm.api.auth import Recruiter
from rvm.ingest import load_text

API = "https://api.hh.ru"
EMPLOYER_ID = os.environ.get("RVM_HH_EMPLOYER", "1008541")  # МФТИ: hh.ru/employer/1008541
USER_AGENT = os.environ.get("RVM_HH_USER_AGENT", "rvm/0.1")
CACHE_S = 24 * 3600
MAX_PAGES = 20  # по 100 вакансий
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<\s*(br|/p|/li|/h\d|/div)\s*/?>", re.IGNORECASE)


def fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    r = httpx.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "HH-User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def to_text(item: dict[str, Any]) -> str:
    """Название, описание без разметки и ключевые навыки — текст вакансии для разбора."""
    desc = re.sub(r"<\s*li[^>]*>", "\n• ", item.get("description") or "", flags=re.IGNORECASE)
    desc = html.unescape(_TAG.sub("", _BREAK.sub("\n", desc)))
    skills = ", ".join(s["name"] for s in item.get("key_skills") or [])
    lines = [item["name"], *desc.splitlines(), f"Ключевые навыки: {skills}" if skills else ""]
    return "\n".join(ln.strip() for ln in lines if ln.strip())


class HHVacancy(BaseModel):
    id: str
    name: str
    url: str
    area: str
    published_at: str
    imported: bool  # уже есть в базе


class ImportBody(BaseModel):
    ids: list[Annotated[str, Field(pattern=r"^\d{1,12}$")]] = Field(min_length=1, max_length=50)


class Imported(BaseModel):
    added: list[str]  # id вакансий в базе
    skipped: list[str]  # id hh, уже импортированные
    errors: dict[str, str]


def router(db: Path, on_added: Callable[[str], None]) -> APIRouter:
    """on_added(id) — после импорта вакансии (сверка резерва, как при ручном добавлении)."""
    r = APIRouter()
    cache: dict[str, Any] = {"at": 0.0, "items": []}
    lock = threading.Lock()

    def imported() -> set[str]:
        return {v["source"].removeprefix("hh:") for v in store.list_vacancies(db)}

    def listing() -> list[dict[str, Any]]:
        with lock:
            if time.monotonic() - cache["at"] < CACHE_S and cache["at"]:
                return list(cache["items"])
            items, page, pages = [], 0, 1
            while page < min(pages, MAX_PAGES):
                data = fetch_json(
                    f"{API}/vacancies",
                    {"employer_id": EMPLOYER_ID, "per_page": 100, "page": page},
                )
                items += data.get("items", [])
                pages, page = data.get("pages", 1), page + 1
            cache.update(at=time.monotonic(), items=items)
            return list(items)

    @r.get(
        "/api/vacancies/hh",
        summary="Открытые вакансии МФТИ на hh.ru",
        description=f"Работодатель {EMPLOYER_ID} (RVM_HH_EMPLOYER), ответ hh кэшируется на сутки.",
    )
    def hh_list(_: Recruiter) -> list[HHVacancy]:
        try:
            items = listing()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"hh.ru недоступен: {e}") from e
        done = imported()
        return [
            HHVacancy(
                id=str(v["id"]),
                name=v["name"],
                url=v.get("alternate_url") or "",
                area=(v.get("area") or {}).get("name", ""),
                published_at=v.get("published_at") or "",
                imported=str(v["id"]) in done,
            )
            for v in items
        ]

    @r.post("/api/vacancies/hh/import", summary="Импорт выбранных вакансий hh в базу")
    def hh_import(body: ImportBody, _: Recruiter) -> Imported:
        out = Imported(added=[], skipped=[], errors={})
        done = imported()
        for hid in dict.fromkeys(body.ids):
            if hid in done:
                out.skipped.append(hid)
                continue
            try:
                item = fetch_json(f"{API}/vacancies/{hid}")
            except httpx.HTTPError as e:
                out.errors[hid] = f"hh.ru: {e}"
                continue
            item["name"] = item.get("name") or f"Вакансия hh {hid}"
            doc = load_text(item["name"], to_text(item))
            vid = store.add_vacancy(db, doc, source=f"hh:{hid}")
            on_added(vid)
            out.added.append(vid)
        return out

    return r
