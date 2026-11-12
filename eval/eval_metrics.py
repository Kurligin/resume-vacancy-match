"""Метрики замера качества: эталон (case.yaml) против отчётов конвейера (Report в виде dict)."""

from typing import Any

from rapidfuzz import fuzz

STATUSES = ("full", "partial", "no", "unknown")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _similarity(gold: dict[str, Any], text: str) -> float:
    """Похожесть извлечённого требования на эталонное: лучшая из формулировок.
    token_set щедр к подмножеству слов, token_sort штрафует за разницу длины — берём среднее."""
    t = _norm(text)
    return max(
        (fuzz.token_set_ratio(_norm(v), t) + fuzz.token_sort_ratio(_norm(v), t)) / 2
        for v in [gold["text"], *gold.get("variants", [])]
    )


def match_requirements(
    gold_reqs: list[dict[str, Any]], extracted: list[dict[str, Any]], threshold: float = 60
) -> dict[str, str]:
    """Эталонный ключ → id извлечённого требования. Один к одному, жадно по убыванию похожести."""
    pairs = sorted(
        ((_similarity(g, e["text"]), g["key"], e["id"]) for g in gold_reqs for e in extracted),
        reverse=True,
    )
    out: dict[str, str] = {}
    used: set[str] = set()
    for score, key, rid in pairs:
        if score >= threshold and key not in out and rid not in used:
            out[key] = rid
            used.add(rid)
    return out


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def evaluate(gold: dict[str, Any], reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """reports: метка резюме из эталона → Report.model_dump(mode="json"); id вакансий в отчёте —
    ключи эталона. Резюме без отчёта считается промахом в рейтинге и переадресации."""
    gvacs = gold["vacancies"]
    m: dict[str, Any] = {}

    # Извлечение требований: по первому отчёту, где вакансия есть (требования общие для всех).
    found = total = extra = sec_ok = 0
    for vid, spec in gvacs.items():
        vac = next((v for r in reports.values() for v in r["vacancies"] if v["id"] == vid), None)
        extracted = vac["requirements"] if vac else []
        mapping = match_requirements(spec["requirements"], extracted)
        by_id = {e["id"]: e for e in extracted}
        total += len(spec["requirements"])
        found += len(mapping)
        extra += len(extracted) - len(mapping)
        for g in spec["requirements"]:
            if g["key"] in mapping:
                allowed = [g["section"], *g.get("section_alt", [])]
                sec_ok += by_id[mapping[g["key"]]]["section"] in allowed
    m.update(
        req_found=found,
        req_total=total,
        req_extra=extra,
        req_recall=_ratio(found, total),
        section_acc=_ratio(sec_ok, found),
    )

    # Статусы, цитаты, рейтинг, переадресация, стаж.
    confusion = {g: {p: 0 for p in STATUSES} for g in STATUSES}
    strict = lenient = pairs = unmatched = 0
    q_ok = q_bad = 0
    top1 = order_ok = redirect_ok = 0
    disc = [0, 0]
    snap = [0, 0]
    errors: list[dict[str, Any]] = []
    for res in gold["resumes"]:
        rep = reports.get(res["label"])
        if rep is None:
            unmatched += sum(len(s) for s in res["statuses"].values())
            continue
        vacs = {v["id"]: v for v in rep["vacancies"]}
        for vid, expected in res["statuses"].items():
            vac = vacs.get(vid)
            if vac is None:
                unmatched += len(expected)
                continue
            for r in vac["rows"]:
                has_q = bool(r["evidence"]) and "quote_not_found" not in r["flags"]
                q_ok += has_q
                q_bad += "quote_not_found" in r["flags"]
            mapping = match_requirements(gvacs[vid]["requirements"], vac["requirements"])
            rows = {r["requirement_id"]: r["status"] for r in vac["rows"]}
            texts = {g["key"]: g["text"] for g in gvacs[vid]["requirements"]}
            for key, exp in expected.items():
                got = rows.get(mapping.get(key, ""))
                if got is None:
                    unmatched += 1
                    continue
                pairs += 1
                confusion[exp["s"]][got] += 1
                strict += got == exp["s"]
                lenient += got in [exp["s"], *exp.get("alt", [])]
                if got != exp["s"]:
                    errors.append(
                        {
                            "resume": res["label"],
                            "vacancy": vid,
                            "requirement": texts[key],
                            "expected": exp["s"],
                            "alt": exp.get("alt", []),
                            "got": got,
                        }
                    )
        order = [v["id"] for v in sorted(rep["vacancies"], key=lambda v: v["place"] or 99)]
        top1 += bool(order) and order[0] == res["order"][0]
        order_ok += order == res["order"]
        r = rep.get("redirect")
        got_redirect = {"from": r["from_vacancy"], "to": r["to_vacancy"]} if r else None
        redirect_ok += got_redirect == res["redirect"]
        facts = rep.get("facts") or {}
        if res.get("discrepancy") is not None:
            disc[1] += 1
            disc[0] += bool(facts.get("discrepancies")) == res["discrepancy"]
        if res.get("snapshot_note") is not None:
            snap[1] += 1
            snap[0] += res["snapshot_note"] in (facts.get("snapshot_note") or "")

    n = len(gold["resumes"])
    pred_no = sum(confusion[g]["no"] for g in STATUSES)
    gold_no = sum(confusion["no"].values())
    m.update(
        status_pairs=pairs,
        status_unmatched=unmatched,
        status_strict=_ratio(strict, pairs),
        status_lenient=_ratio(lenient, pairs),
        no_precision=_ratio(confusion["no"]["no"], pred_no),
        no_recall=_ratio(confusion["no"]["no"], gold_no),
        no_as_unknown=confusion["no"]["unknown"],
        unknown_as_no=confusion["unknown"]["no"],
        quotes_verified=_ratio(q_ok, q_ok + q_bad),
        top1=_ratio(top1, n),
        order_exact=_ratio(order_ok, n),
        redirect_acc=_ratio(redirect_ok, n),
        discrepancy_acc=_ratio(*disc),
        snapshot_acc=_ratio(*snap),
        confusion=confusion,
        errors=errors,
    )
    return m
