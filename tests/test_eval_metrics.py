"""Метрики замера качества на синтетике: эталон 2 резюме, фейковый вывод конвейера."""

from typing import Any

import pytest
from eval_metrics import STATUSES, evaluate, match_requirements

GOLD: dict[str, Any] = {
    "vacancies": {
        "va": {
            "requirements": [
                {
                    "key": "k1",
                    "section": "must",
                    "text": "Опыт коммерческой разработки на Vue.js",
                    "variants": ["Vue.js коммерческая разработка"],
                },
                {"key": "k2", "section": "nice", "text": "Знание Figma"},
            ]
        },
        "vb": {
            "requirements": [
                {"key": "k3", "section": "must", "text": "Высшее экономическое образование"}
            ]
        },
    },
    "resumes": [
        {
            "label": "Р1",
            "order": ["va", "vb"],
            "redirect": None,
            "discrepancy": True,
            "snapshot_note": "2025",
            "statuses": {
                "va": {"k1": {"s": "no"}, "k2": {"s": "unknown", "alt": ["partial"]}},
                "vb": {"k3": {"s": "unknown"}},
            },
        },
        {
            "label": "Р2",
            "order": ["vb", "va"],
            "redirect": {"from": "va", "to": "vb"},
            "discrepancy": None,
            "snapshot_note": None,
            "statuses": {
                "va": {"k1": {"s": "full"}, "k2": {"s": "full"}},
                "vb": {"k3": {"s": "no", "alt": ["unknown"]}},
            },
        },
    ],
}

VA_REQS = [
    {"id": "r1", "text": "Коммерческий опыт разработки на Vue.js", "section": "must"},
    {"id": "r2", "text": "Опыт работы с Docker", "section": "nice"},
]
VB_REQS = [{"id": "r1", "text": "Высшее экономическое образование", "section": "nice"}]
OK_QUOTE = [{"line": 3, "quote": "Vue"}]


def row(rid: str, status: str, evidence: list | None = None, flags: list | None = None) -> dict:
    return {
        "requirement_id": rid,
        "status": status,
        "evidence": evidence or [],
        "flags": flags or [],
    }


def report(va_rows: list, vb_rows: list, places: dict, **extra: Any) -> dict:
    vacs = [
        {"id": "va", "requirements": VA_REQS, "rows": va_rows, "place": places["va"]},
        {"id": "vb", "requirements": VB_REQS, "rows": vb_rows, "place": places["vb"]},
    ]
    vacs.sort(key=lambda v: v["place"])
    return {
        "vacancies": vacs,
        "redirect": extra.get("redirect"),
        "facts": extra.get("facts", {"discrepancies": [], "snapshot_note": None}),
    }


REPORTS = {
    "Р1": report(
        [row("r1", "no", OK_QUOTE), row("r2", "unknown")],
        [row("r1", "no", OK_QUOTE, ["quote_not_found"])],
        {"va": 1, "vb": 2},
        facts={
            "discrepancies": [{"declared": 24}],
            "snapshot_note": "резюме, похоже, от марта 2025",
        },
    ),
    "Р2": report(
        [row("r1", "full"), row("r2", "full")], [row("r1", "unknown")], {"va": 1, "vb": 2}
    ),
}


def test_match_requirements_fuzzy_one_to_one() -> None:
    got = match_requirements(GOLD["vacancies"]["va"]["requirements"], VA_REQS)
    assert got == {"k1": "r1"}  # Figma не извлечена, Docker — лишнее


def test_evaluate_numbers() -> None:
    m = evaluate(GOLD, REPORTS)
    assert (m["req_found"], m["req_total"], m["req_extra"]) == (2, 3, 1)
    assert m["req_recall"] == pytest.approx(2 / 3)
    assert m["section_acc"] == 0.5
    assert (m["status_pairs"], m["status_unmatched"]) == (4, 2)
    assert m["status_strict"] == 0.5
    assert m["status_lenient"] == 0.75
    assert m["no_precision"] == 0.5
    assert m["no_recall"] == 0.5
    assert (m["no_as_unknown"], m["unknown_as_no"]) == (1, 1)
    assert m["quotes_verified"] == 0.5
    assert m["top1"] == 0.5
    assert m["order_exact"] == 0.5
    assert m["redirect_acc"] == 0.5
    assert m["discrepancy_acc"] == 1.0
    assert m["snapshot_acc"] == 1.0
    assert m["confusion"]["unknown"]["no"] == 1
    assert len(m["errors"]) == 2  # строгие расхождения: Р1 k3, Р2 k3


def test_evaluate_empty_report_counts_as_miss() -> None:
    m = evaluate(GOLD, {"Р1": REPORTS["Р1"]})  # Р2 упал — нет отчёта
    assert m["top1"] == 0.5
    assert m["redirect_acc"] == 0.5


def test_case_gold_covers_all_pairs_and_matches_itself() -> None:
    from pathlib import Path

    import yaml

    gold = yaml.safe_load((Path(__file__).parents[1] / "eval/gold/case.yaml").read_text())
    assert len(gold["resumes"]) * len(gold["vacancies"]) == 12
    for res in gold["resumes"]:
        assert set(res["order"]) == set(gold["vacancies"])
        for vid, spec in gold["vacancies"].items():
            assert {g["key"] for g in spec["requirements"]} == set(res["statuses"][vid])
            for exp in res["statuses"][vid].values():
                assert {exp["s"], *exp.get("alt", [])} <= set(STATUSES)
    for spec in gold["vacancies"].values():
        reqs = spec["requirements"]
        as_extracted = [{"id": g["key"], "text": g["text"]} for g in reqs]
        assert match_requirements(reqs, as_extracted) == {g["key"]: g["key"] for g in reqs}


def test_render_and_usage(tmp_path: Any) -> None:
    from run_eval import render, usage_since

    log = tmp_path / "usage.jsonl"
    log.write_text('{"prompt_tokens": 5, "completion_tokens": 1}\n' * 3)
    assert usage_since(log, 1) == {"calls": 2, "prompt_tokens": 10, "completion_tokens": 2}
    m = evaluate(GOLD, {"Р1": REPORTS["Р1"]})
    m.update(model="m", today="2026-10-15", elapsed_s=1.0, usage=usage_since(log, 0))
    m["rankings"] = {"Р1": {"order": ["va", "vb"], "redirect": None}}
    text = render(m, GOLD, {"va": "Вакансия А", "vb": "Вакансия Б"})
    assert "| Р1 | Вакансия А > Вакансия Б | Вакансия А > Вакансия Б | нет |" in text
    assert "| Р2 | Вакансия Б > Вакансия А | ошибка | нет |" in text
