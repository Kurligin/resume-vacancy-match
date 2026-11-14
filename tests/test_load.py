"""Засев нагрузки eval/load_seed.py: отдельная база с N кандидатами, список её видит."""

from pathlib import Path

import load_seed
import pytest
from load_bench import client


def test_seed_n_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RVM_HASH_SALT", "s" * 32)
    monkeypatch.setenv("RVM_SECRET", "k" * 32)
    monkeypatch.setenv("RVM_DEMO_LOGIN", "1")  # client() ставит его в окружение — вернуть после
    db = tmp_path / "load.sqlite"
    assert load_seed.seed(db, 50) == 50
    with client(db) as api:
        page = api.get("/api/candidates?page=1").json()
        assert page["total"] == 50 and len(page["items"]) == 50
        assert sum(map(len, (api.get("/api/triage").json()[b] for b in ("need", "fit", "nofit"))))
    with pytest.raises(SystemExit):  # существующую базу не трогает
        load_seed.seed(db, 1)
    with pytest.raises(SystemExit):  # и в var/ репозитория не пишет
        load_seed.seed(load_seed.ROOT / "var" / "load.sqlite", 1)
