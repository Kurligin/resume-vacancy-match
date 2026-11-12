"""Дополняет справочник навыков написаниями из подсказок hh (`suggests/skill_set`).

Запуск: `uv run python scripts/build_skill_refs.py`. Единственное место, где справочник
ходит в сеть; рантайм читает только `src/rvm/refs/data/skills.json`.
Подсказка становится синонимом (`hh_synonyms`), только если после отбрасывания цифр и
префикса MS/Microsoft совпадает с уже известным написанием навыка: «1С: Предприятие 8»,
«MS Excel». Остальные подсказки hh — соседние навыки по префиксу («Java» → «JavaScript»),
смежными их не делаем.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from rvm.refs import skills

URL = "https://api.hh.ru/suggests/skill_set?text="
PAUSE = 0.35


def _bare(term: str) -> str:
    return re.sub(r"^(microsoft|ms)|\d", "", skills.key(term))


def pick_synonyms(
    name: str, known: list[str], suggestions: list[str], canonical: Callable[[str], str | None]
) -> list[str]:
    """Новые написания навыка среди подсказок hh. «Ms SQL 2000» для SQL — чужой навык."""
    bare = {_bare(t) for t in known if "*" not in t} - {""}
    return [
        s
        for s in suggestions
        if canonical(s) is None
        and canonical(re.sub(r"\d", "", s)) in (None, name)
        and _bare(s) in bare
    ]


def suggest(text: str) -> list[str]:
    req = urllib.request.Request(
        URL + urllib.parse.quote(text), headers={"User-Agent": "rvm-refs/0.1"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return [item["text"] for item in json.load(resp).get("items", [])]


def main() -> None:
    data = json.loads(skills.DATA.read_text(encoding="utf-8"))
    added = 0
    for item in data:
        try:
            found = suggest(item["name"])
        except OSError as exc:
            print(f"{item['name']}: hh недоступен ({exc})")
            found = []
        time.sleep(PAUSE)
        known = [item["name"], *item["synonyms"], *item.get("hh_synonyms", [])]
        new = list(dict.fromkeys(pick_synonyms(item["name"], known, found, skills.canonical)))
        if new:
            item["hh_synonyms"] = [*item.get("hh_synonyms", []), *new]
            added += len(new)
            print(f"{item['name']}: {new}")
    lines = (json.dumps(e, ensure_ascii=False) for e in data)
    skills.DATA.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")
    print(f"добавлено написаний: {added}")


if __name__ == "__main__":
    main()
