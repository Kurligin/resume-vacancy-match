"""Запуск в контейнере: ключи по умолчанию и демо-данные в пустую базу."""

import importlib.util
import sqlite3
import time
from pathlib import Path
from types import ModuleType

import pytest

from rvm import store

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"


def _start() -> ModuleType:
    spec = importlib.util.spec_from_file_location("start", ROOT / "scripts" / "start.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keys_generated_once_and_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = _start()
    monkeypatch.delenv("RVM_HASH_SALT", raising=False)
    monkeypatch.setenv("RVM_SECRET", "задан-в-окружении")
    start.ensure_keys(tmp_path)
    salt = start.os.environ["RVM_HASH_SALT"]
    assert len(salt) >= 16
    assert start.os.environ["RVM_SECRET"] == "задан-в-окружении"
    assert "RVM_SECRET" not in (tmp_path / "keys.env").read_text()
    monkeypatch.delenv("RVM_HASH_SALT")
    start.ensure_keys(tmp_path)  # перезапуск контейнера — та же соль, дубли склеиваются
    assert start.os.environ["RVM_HASH_SALT"] == salt


def test_seed_demo_into_empty_db_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = _start()
    monkeypatch.setenv("RVM_HASH_SALT", "s" * 32)
    monkeypatch.setenv("RVM_SECRET", "k" * 32)
    db, cache = tmp_path / "rvm.sqlite", tmp_path / "cache.sqlite"
    assert start.seed(db, DEMO, cache)
    assert len(store.list_vacancies(db)) == 3
    assert len(store.list_candidates(db)) == 3
    assert not start.seed(db, DEMO, cache)  # база не пустая — повторно не грузим
    assert len(store.list_candidates(db)) == 3


def test_demo_cache_copied_fresh(tmp_path: Path) -> None:
    start = _start()
    demo, cache = tmp_path / "demo", tmp_path / "var" / "cache.sqlite"
    demo.mkdir()
    with sqlite3.connect(demo / "cache.sqlite") as c:
        c.execute("CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, created REAL)")
        c.execute("INSERT INTO cache VALUES ('k', 'v', 0)")
    start.copy_cache(demo, cache)
    with sqlite3.connect(cache) as c:
        (created,) = c.execute("SELECT created FROM cache").fetchone()
    assert created > time.time() - 60  # срок жизни кэша отсчитывается от старта
