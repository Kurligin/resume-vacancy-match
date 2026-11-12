"""Живой прогон конвейера: все резюме × все вакансии из папки → рейтинг в консоль.

Запуск из корня репозитория:
    uv run python scripts/live_smoke.py [--model ID] [--data DIR] [--env FILE]
Ключ берётся из env-файла (по умолчанию RVM_ENV_FILE) и никуда не выводится.
"""

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

DEFAULT_ENV = "~/Downloads/hackathons-heavy/resume-vacancy-match/.env"
DEFAULT_DATA = "~/Downloads/hackathons-heavy/resume-vacancy-match/data/case"
DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"  # тот же класс, что Qwen3.6 35B в Yandex AI Studio


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--data", default=os.environ.get("RVM_DATA_DIR", DEFAULT_DATA))
    ap.add_argument("--env", default=os.environ.get("RVM_ENV_FILE", DEFAULT_ENV))
    ap.add_argument("--today", default=None, help="ГГГГ-ММ-ДД, по умолчанию сегодня")
    args = ap.parse_args()

    load_env(Path(args.env).expanduser())
    os.environ["LLM_MODEL"] = args.model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    if not os.environ.get("LLM_API_KEY"):
        print("Нет LLM_API_KEY в env-файле", file=sys.stderr)
        return 1

    from rvm.ingest import load_path
    from rvm.llm.client import LLMClient
    from rvm.pipeline.extract_resume import extract_resume_facts
    from rvm.pipeline.extract_vacancy import extract_requirements
    from rvm.pipeline.match import match
    from rvm.pipeline.verify import verified_share
    from rvm.scoring.rank import rank_vacancies
    from rvm.scoring.score import score_pair

    today = date.fromisoformat(args.today) if args.today else date.today()
    llm = LLMClient()
    files = sorted(Path(args.data).expanduser().glob("*.docx"))
    vac_files = [f for f in files if "вакансия" in f.name.lower()]
    res_files = [f for f in files if f not in vac_files]
    print(
        f"Модель: {os.environ['LLM_MODEL']} · резюме {len(res_files)} · вакансий {len(vac_files)}"
    )

    t0 = time.time()
    vacancies = {}
    for f in vac_files:
        doc = load_path(f)
        reqs = extract_requirements(doc, llm)
        vacancies[f.stem] = reqs
        print(f"  {f.stem}: требований {len(reqs)}")

    shares = []
    for f in res_files:
        doc = load_path(f)
        facts = extract_resume_facts(doc, llm)
        scores = {}
        for name, reqs in vacancies.items():
            rows = match(reqs, doc, facts, llm, today)
            shares.append(verified_share(rows))
            scores[name] = score_pair(reqs, rows)
        ranking = rank_vacancies(scores)
        print(f"\n{f.stem}")
        for p in ranking.places:
            s = p.score
            bl = f" · блокеры {len(s.blockers)}" if s.blockers else ""
            print(
                f"  {p.place}. {p.id:<12} {s.score:5.1f} [{s.low:.0f}–{s.high:.0f}] {s.verdict}{bl}"
            )
        if ranking.none_fit:
            print("  ни одна вакансия не подходит")

    share = sum(shares) / len(shares) if shares else 0.0
    print(f"\nДоля проверенных цитат: {share:.0%} · время {time.time() - t0:.0f} с")
    print("Расход токенов — var/usage.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
