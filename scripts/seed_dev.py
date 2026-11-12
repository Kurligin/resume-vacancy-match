"""Заполнить базу для показа интерфейса без ключа: вакансии и резюме через API с фейковой
моделью (rvm.llm.fake.DemoFake). Сеть не используется.

    RVM_HASH_SALT=... RVM_DB=/tmp/rvm/rvm.sqlite uv run python scripts/seed_dev.py \\
        --vacancy вакансия.docx --resume резюме1.docx резюме2.docx

Без --vacancy/--resume берёт файлы из RVM_DATA_DIR: в имени «ваканс» — вакансия."""

import argparse
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from rvm.api import create_app
from rvm.llm import LLMClient
from rvm.llm.fake import DemoFake


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vacancy", nargs="*", type=Path, default=[])
    ap.add_argument("--resume", nargs="*", type=Path, default=[])
    args = ap.parse_args()
    if not args.vacancy and not args.resume:
        files = sorted(p for p in Path(os.environ["RVM_DATA_DIR"]).iterdir() if p.is_file())
        args.vacancy = [p for p in files if "ваканс" in p.name.lower()]
        args.resume = [p for p in files if p not in args.vacancy]
    db = Path(os.environ.get("RVM_DB", "var/rvm.sqlite"))
    llm = LLMClient(
        DemoFake(), base_url="https://fake.invalid", model="demo", cache_path=db.parent / "seed.c"
    )
    with TestClient(create_app(db=db, llm=llm)) as api:
        for p in args.vacancy:
            r = api.post("/api/vacancies", files={"file": (p.name, p.read_bytes())})
            print("вакансия:", p.name, r.status_code)
        if args.resume:
            up = [("files", (p.name, p.read_bytes())) for p in args.resume]
            bid = api.post("/api/uploads", files=up).json()["batch_id"]
            while True:
                files = api.get(f"/api/uploads/{bid}").json()["files"]
                if all(f["state"] in ("done", "error") for f in files):
                    break
                time.sleep(0.2)
            for f in files:
                print("резюме:", f["name"], f["state"], f["message"])
    print("база:", db)


if __name__ == "__main__":
    main()
