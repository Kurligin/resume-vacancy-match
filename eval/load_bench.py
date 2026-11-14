"""Замер нагрузки: время ответа API на синтетических базах eval/load_seed.py → eval/load.md.

    uv run python eval/load_bench.py --db БАЗА [БАЗА ...] [--runs 30] [--out eval/load.md]

Приложение поднимается в процессе (TestClient, демо-модель, кэш рядом с базой), вход —
рекрутером по демо-входу. Запросы последовательные: 3 прогревочных, затем --runs замеров на
маршрут; p50/p95 — по времени запроса с разбором JSON в TestClient."""

import argparse
import os
import platform
import sqlite3
import statistics
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from rvm.api import create_app
from rvm.llm import LLMClient
from rvm.llm.fake import DemoFake

HERE = Path(__file__).resolve().parent
WARMUP = 3


@contextmanager
def client(db: Path) -> Iterator[TestClient]:
    """Приложение на базе db в демо-режиме, вошли рекрутером."""
    os.environ["RVM_DEMO_LOGIN"] = "1"
    llm = LLMClient(DemoFake(), mode="demo", cache_path=db.parent / f"{db.stem}-bench-cache.sqlite")
    app = create_app(db=db, llm=llm)
    with TestClient(app, base_url="http://localhost", headers={"X-Requested-With": "load"}) as api:
        api.post("/api/login", json={"role": "recruiter"}).raise_for_status()
        yield api


def routes(api: TestClient) -> dict[str, str]:
    vid = api.get("/api/vacancies").json()[0]["id"]
    return {
        "список, страница": "/api/candidates?page=1",
        "список, вакансия + вердикт": f"/api/candidates?page=1&vacancy={vid}&verdict=fit,partial",
        "список, поиск": "/api/candidates?page=1&q=figma",
        "список целиком (старый)": "/api/candidates",
        "вакансии со счётчиками": "/api/vacancies",
        "разбор потока целиком": "/api/triage",
        "разбор потока, по 50 в корзине": "/api/triage?limit=50",
        "счётчики шапки": "/api/stats",
    }


def measure(api: TestClient, url: str, runs: int) -> tuple[float, float, int]:
    """p50 и p95 в мс, размер ответа в байтах."""
    times, size = [], 0
    for i in range(WARMUP + runs):
        t0 = time.perf_counter()
        r = api.get(url)
        r.raise_for_status()
        r.json()
        if i >= WARMUP:
            times.append((time.perf_counter() - t0) * 1000)
        size = len(r.content)
    q = statistics.quantiles(times, n=20, method="inclusive")
    return statistics.median(times), q[18], size


def machine() -> str:
    cpu = platform.processor() or platform.machine()
    if platform.system() == "Darwin":
        cpu = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    return (
        f"{cpu}, {os.cpu_count()} ядер, {platform.system()} {platform.release()}, "
        f"Python {platform.python_version()}, SQLite {sqlite3.sqlite_version}"
    )


def bench(db: Path, runs: int) -> str:
    with closing(sqlite3.connect(db)) as c:
        n = c.execute("SELECT count(*) FROM candidates").fetchone()[0]
    mb = db.stat().st_size / 2**20
    lines = [
        f"## {n} кандидатов (база {mb:.0f} МБ)",
        "",
        "| Запрос | p50, мс | p95, мс | Ответ, КБ |",
        "|---|---:|---:|---:|",
    ]
    with client(db) as api:
        for name, url in routes(api).items():
            p50, p95, size = measure(api, url, runs)
            lines.append(f"| {name} `{url}` | {p50:.0f} | {p95:.0f} | {size / 1024:.0f} |")
            print(lines[-1], flush=True)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--out", type=Path, default=HERE / "load.md")
    args = ap.parse_args()
    parts = [
        "# Нагрузка: время ответа API",
        "",
        (
            f"{date.today()}, {machine()}. Базы — `eval/load_seed.py` (копии разборов демо, "
            f"random.Random(42)); запросы последовательные в одном процессе (TestClient), "
            f"{WARMUP} прогревочных и {args.runs} замеров на маршрут, вход рекрутером."
        ),
    ]
    parts += ["", *(bench(db, args.runs) + "\n" for db in args.db)]
    args.out.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    print("записано:", args.out)


if __name__ == "__main__":
    main()
