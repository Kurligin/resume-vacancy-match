"""Запуск в контейнере: ключи по умолчанию, демо-данные в пустую базу, затем `rvm serve`.

RVM_HASH_SALT и RVM_SECRET не заданы — генерируются один раз и хранятся в <var>/keys.env.
RVM_SEED=1 — при пустой базе загрузить вакансии и резюме из RVM_DEMO_DIR (по умолчанию
data/demo): в имени файла «ваканс» — вакансия, остальное — отклики на первую вакансию.
data/demo/cache.sqlite (ответы модели живого прогона) копируется в кэш, если его ещё нет."""

import logging
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from rvm import store
from rvm.api import auth, create_app
from rvm.llm import LLMClient
from rvm.llm.fake import DemoFake

DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"

log = logging.getLogger("rvm.start")
KEYS = ("RVM_HASH_SALT", "RVM_SECRET")


def ensure_keys(var: Path) -> None:
    """Пустые ключи — из <var>/keys.env или новые случайные (соль стабильна между запусками)."""
    path = var / "keys.env"
    saved = dict(s.split("=", 1) for s in path.read_text().split()) if path.exists() else {}
    for name in KEYS:
        if os.environ.get(name):
            continue
        saved.setdefault(name, secrets.token_hex(32))
        os.environ[name] = saved[name]
        log.warning("%s не задан — сгенерирован в %s; для стенда задайте свой", name, path)
    if saved:
        var.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in saved.items()))
        path.chmod(0o600)


def copy_cache(demo: Path, cache: Path) -> None:
    """Ответы модели из демо-кэша; срок жизни кэша отсчитывается заново."""
    src = demo / "cache.sqlite"
    if not src.exists() or cache.exists():
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, cache)
    with closing(sqlite3.connect(cache)) as db, db:
        db.execute("UPDATE cache SET created = ?", (time.time(),))


def seed(db: Path, demo: Path, cache: Path) -> bool:
    """Загрузить демо-данные, если в базе нет вакансий. True — загрузили."""
    store.init(db)
    if store.list_vacancies(db):
        return False
    files = sorted(p for p in demo.iterdir() if p.is_file() and p.suffix != ".sqlite")
    vacancies = [p for p in files if "ваканс" in p.name.lower()]
    resumes = [p for p in files if p not in vacancies]
    # отдельный кэш: ответы фейковой модели не должны попасть в рабочий
    seed_cache = cache.parent / "seed-cache.sqlite"
    copy_cache(demo, seed_cache)
    # имя модели — ключ demo-кэша: живые ответы лежат под моделью по умолчанию
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    llm = LLMClient(
        DemoFake(), base_url="https://fake.invalid", model=model, mode="live", cache_path=seed_cache
    )
    secret = os.environ["RVM_SECRET"].encode()
    with TestClient(
        create_app(db=db, llm=llm),
        base_url="http://localhost",
        headers={"X-Requested-With": "rvm-seed"},
        cookies={auth.COOKIE: auth.issue(secret, auth.demo_user("recruiter"))},
    ) as api:
        ids = []
        for p in vacancies:
            r = api.post("/api/vacancies", files={"file": (p.name, p.read_bytes())})
            r.raise_for_status()
            ids.append(r.json()["id"])
        if resumes:
            up = [("files", (p.name, p.read_bytes())) for p in resumes]
            r = api.post("/api/uploads", files=up, params={"applied": ids[0]} if ids else {})
            r.raise_for_status()
            bid = r.json()["batch_id"]
            deadline = time.time() + 300
            while time.time() < deadline:
                states = api.get(f"/api/uploads/{bid}").json()["files"]
                if all(f["state"] in ("done", "error") for f in states):
                    break
                time.sleep(0.2)
            for f in states:
                log.warning("демо-резюме %s: %s %s", f["name"], f["state"], f["message"] or "")
    log.warning("демо-данные загружены: %d вакансий, %d резюме", len(vacancies), len(resumes))
    return True


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    db = Path(os.environ.get("RVM_DB", "var/rvm.sqlite"))
    cache = Path(os.environ.get("RVM_CACHE", "var/cache.sqlite"))
    demo = Path(os.environ.get("RVM_DEMO_DIR", "data/demo"))
    ensure_keys(db.parent)
    copy_cache(demo, cache)
    if os.environ.get("RVM_SEED") == "1":
        seed(db, demo, cache)
    os.execvp("rvm", ["rvm", "serve", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    main()
