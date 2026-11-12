"""Замер качества: конвейер на файлах эталона → метрики → eval/report.md и eval/last.json.

Запуск из корня репозитория:
    uv run python eval/run_eval.py [--model ID] [--data DIR] [--env FILE] [--today ГГГГ-ММ-ДД]
Ответы модели кэшируются (var/cache.sqlite): повторный прогон той же модели идёт из кэша.
Ключ берётся из env-файла и никуда не выводится.
"""

import argparse
import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from eval_metrics import STATUSES, evaluate

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = "~/Downloads/hackathons-heavy/resume-vacancy-match/.env"
DEFAULT_DATA = "~/Downloads/hackathons-heavy/resume-vacancy-match/data/case"
DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"
STATUS_RU = {"full": "есть", "partial": "частично", "no": "нет", "unknown": "не указано"}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def usage_since(path: Path, offset: int) -> dict[str, int]:
    """Вызовы и токены из журнала расхода начиная со строки offset (ответы из кэша не пишутся)."""
    lines = path.read_text(encoding="utf-8").splitlines()[offset:] if path.exists() else []
    recs = [json.loads(ln) for ln in lines if ln.strip()]
    return {
        "calls": len(recs),
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in recs),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in recs),
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def render(m: dict[str, Any], gold: dict[str, Any], vac_names: dict[str, str]) -> str:
    rows = [
        (
            "Полнота извлечения требований",
            (
                f"{_pct(m['req_recall'])} ({m['req_found']} из "
                f"{m['req_total']}; сверх эталона {m['req_extra']})"
            ),
        ),
        ("Точность раздела требования", _pct(m["section_acc"])),
        (
            "Точность статусов, строго",
            (
                f"{_pct(m['status_strict'])} ({m['status_pairs']} пар; "
                f"без пары {m['status_unmatched']})"
            ),
        ),
        ("Точность статусов с допустимыми альтернативами", _pct(m["status_lenient"])),
        ("«Нет»: точность", _pct(m["no_precision"])),
        ("«Нет»: полнота", _pct(m["no_recall"])),
        ("«Нет» → «не указано»", str(m["no_as_unknown"])),
        ("«Не указано» → «нет»", str(m["unknown_as_no"])),
        ("Доля проверенных цитат", _pct(m["quotes_verified"])),
        ("Первое место совпало", _pct(m["top1"])),
        ("Порядок вакансий совпал целиком", _pct(m["order_exact"])),
        ("Переадресация верна", _pct(m["redirect_acc"])),
        ("Расхождение стажа найдено верно", _pct(m["discrepancy_acc"])),
        ("Пометка о дате резюме верна", _pct(m["snapshot_acc"])),
        ("Время прогона, с", f"{m['elapsed_s']:.0f}"),
        (
            "Вызовы модели / токены (вход + выход)",
            (
                f"{m['usage']['calls']} / "
                f"{m['usage']['prompt_tokens']} + {m['usage']['completion_tokens']}"
            ),
        ),
    ]
    out = [
        "# Замер качества на данных кейса",
        "",
        (
            f"Модель: `{m['model']}` · дата расчёта стажа: {m['today']} · резюме "
            f"{len(gold['resumes'])} × вакансий {len(gold['vacancies'])}."
        ),
        (
            "Эталон — наша ручная разметка ожиданий, не разметка организаторов. Вызовы из кэша "
            "в токены не входят."
        ),
        "",
        "| Метрика | Значение |",
        "|---|---|",
        *(f"| {k} | {v} |" for k, v in rows),
        "",
        "## Статусы: эталон (строки) × ответ системы (столбцы)",
        "",
        "| | " + " | ".join(STATUS_RU[s] for s in STATUSES) + " |",
        "|---" * (len(STATUSES) + 1) + "|",
        *(
            f"| {STATUS_RU[g]} | " + " | ".join(str(m["confusion"][g][p]) for p in STATUSES) + " |"
            for g in STATUSES
        ),
        "",
        "## Рейтинг",
        "",
        "| Резюме | Ожидали | Получили | Переадресация |",
        "|---|---|---|---|",
    ]
    for res in gold["resumes"]:
        got = m["rankings"].get(res["label"])
        exp = " > ".join(vac_names[v] for v in res["order"])
        order = " > ".join(vac_names[v] for v in got["order"]) if got else "ошибка"
        redir = got and got["redirect"]
        redir_s = f"{vac_names[redir['from']]} → {vac_names[redir['to']]}" if redir else "нет"
        out.append(f"| {res['label']} | {exp} | {order} | {redir_s} |")
    out += [
        "",
        "## Расхождения статусов",
        "",
        "| Резюме | Вакансия | Требование | Ждали | Допустимо | Получили |",
        "|---|---|---|---|---|---|",
        *(
            f"| {e['resume']} | {vac_names[e['vacancy']]} | {e['requirement']} | "
            f"{STATUS_RU[e['expected']]} | {', '.join(STATUS_RU[a] for a in e['alt']) or '—'} | "
            f"{STATUS_RU[e['got']]} |"
            for e in m["errors"]
        ),
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер качества конвейера по эталону")
    ap.add_argument("--model", default=None, help="id модели, по умолчанию LLM_MODEL или свой")
    ap.add_argument("--data", default=os.environ.get("RVM_DATA_DIR", DEFAULT_DATA))
    ap.add_argument("--env", default=os.environ.get("RVM_ENV_FILE", DEFAULT_ENV))
    ap.add_argument("--today", default=None, help="ГГГГ-ММ-ДД, по умолчанию дата из эталона")
    ap.add_argument("--gold", default=str(HERE / "gold" / "case.yaml"))
    ap.add_argument("--out", default=str(HERE), help="папка для report.md и last.json")
    args = ap.parse_args()

    gold = yaml.safe_load(Path(args.gold).read_text(encoding="utf-8"))
    load_env(Path(args.env).expanduser())
    # соль нужна только для отпечатков контактов; на замер качества не влияет
    os.environ.setdefault("RVM_HASH_SALT", secrets.token_hex(16))
    os.environ["LLM_MODEL"] = args.model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    if not os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODE", "live") == "live":
        print("Нет LLM_API_KEY в env-файле", file=sys.stderr)
        return 1

    from rvm.ingest import load_path
    from rvm.llm.client import LLMClient
    from rvm.pipeline.analyze import analyze

    today = date.fromisoformat(args.today) if args.today else gold.get("today") or date.today()
    data = Path(args.data).expanduser()
    llm = LLMClient()
    offset = (
        len(llm.usage_path.read_text(encoding="utf-8").splitlines())
        if llm.usage_path.exists()
        else 0
    )
    vacancies = {vid: load_path(data / spec["file"]) for vid, spec in gold["vacancies"].items()}
    vac_names = {vid: Path(spec["file"]).stem for vid, spec in gold["vacancies"].items()}

    t0 = time.time()
    reports: dict[str, dict[str, Any]] = {}

    def one(res: dict[str, Any]) -> None:
        rep = analyze(load_path(data / res["file"]), vacancies, llm, today, source_name=res["file"])
        print(f"{res['label']}: {rep.error or 'готово'} · {rep.elapsed_s:.0f} с")
        if not rep.error:
            reports[res["label"]] = rep.model_dump(mode="json")

    # первое резюме отдельно — чтобы требования вакансий попали в кэш, остальные параллельно
    first, rest = gold["resumes"][:1], gold["resumes"][1:]
    for res in first:
        one(res)
    with ThreadPoolExecutor(max_workers=int(os.environ.get("RVM_EVAL_PARALLEL", "4"))) as pool:
        list(pool.map(one, rest))

    m = evaluate(gold, reports)
    m["model"] = llm.model
    m["today"] = str(today)
    m["elapsed_s"] = round(time.time() - t0, 1)
    m["usage"] = usage_since(llm.usage_path, offset)
    m["rankings"] = {
        label: {
            "order": [v["id"] for v in r["vacancies"]],
            "redirect": r["redirect"]
            and {"from": r["redirect"]["from_vacancy"], "to": r["redirect"]["to_vacancy"]},
        }
        for label, r in reports.items()
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "last.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render(m, gold, vac_names), encoding="utf-8")
    print(
        f"Статусы {_pct(m['status_strict'])} строго, {_pct(m['status_lenient'])} с альтернативами"
        f" · первое место {_pct(m['top1'])} · отчёт {out / 'report.md'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
