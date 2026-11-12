"""Справочники навыков и профстандартов: офлайн, без сети."""

import socket

import pytest

from rvm.models import Line
from rvm.refs import profstandards, skills


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Любая попытка соединения в рантайме справочников — ошибка теста."""

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("справочники не должны ходить в сеть")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def test_canonical_ignores_case_dots_hyphens() -> None:
    assert canonical_not_none("VueJS") == skills.canonical("vue.js")
    assert skills.canonical("Vue") == skills.canonical(" vue-js ")
    assert skills.canonical("учёт ОС") == skills.canonical("Учет основных средств")
    assert skills.canonical("1C") == skills.canonical("1С")
    assert skills.canonical("совершенно неизвестная штука") is None


def canonical_not_none(term: str) -> str:
    c = skills.canonical(term)
    assert c is not None, term
    return c


def test_related() -> None:
    assert skills.related("Vue", "Nuxt")
    assert skills.related("Nuxt.js", "VueJS")
    assert not skills.related("Vue", "1С")
    assert not skills.related("Vue", "неизвестно")


def test_find_in_lines_returns_literal_fragment() -> None:
    lines = [
        Line(n=1, text="Бухгалтер, 3 года"),
        Line(n=2, text="вела учёт ОС и ТМЦ, работала в 1С:Бухгалтерия"),
        Line(n=3, text="Frontend: VueJS, Nuxt.js, TypeScript"),
    ]
    hits = skills.find_in_lines(lines)
    for h in hits:
        assert h.matched in lines[h.line - 1].text
    by_canon = {h.canonical: h for h in hits}
    os_ = by_canon[canonical_not_none("учёт основных средств")]
    assert os_.line == 2 and os_.matched == "учёт ОС"
    assert canonical_not_none("ТМЦ") in by_canon
    assert by_canon[canonical_not_none("Vue")].matched == "VueJS"
    assert canonical_not_none("Nuxt") in by_canon
    assert canonical_not_none("TypeScript") in by_canon


def test_find_prefers_longest_and_respects_word_bounds() -> None:
    hits = skills.find_in_lines([Line(n=1, text="Vue.js и Javascript, оснастка")])
    names = [h.canonical for h in hits]
    assert names.count(canonical_not_none("Vue")) == 1
    assert canonical_not_none("JavaScript") in names
    assert canonical_not_none("Java") not in names


def test_short_upper_abbreviation_is_case_sensitive() -> None:
    assert not skills.find_in_lines([Line(n=1, text="мы ос тались")])


def test_hints() -> None:
    hints = skills.hints("Знание Excel и 1С, опыт с VueJS")
    assert canonical_not_none("Excel") in hints
    assert canonical_not_none("Vue") in hints
    assert len(hints) == len(set(hints))


def test_dictionary_size_and_areas() -> None:
    entries = skills.entries()
    assert 150 <= len(entries) <= 400
    areas = {e.area for e in entries}
    assert {"it", "учёт", "документооборот", "офис", "данные", "педагогика"} <= areas


def test_profstandards() -> None:
    st = profstandards.match_title("Бухгалтер по учёту ОС")
    assert st is not None and st.code == "08.002"
    web = profstandards.match_title("Frontend-разработчик (Vue)")
    assert web is not None and web.code == "06.035"
    dev = profstandards.match_title("Python-разработчик")
    assert dev is not None and dev.code == "06.001"
    assert profstandards.match_title("Космонавт") is None
    assert 5 <= len(profstandards.functions("08.002")) <= 10
    assert profstandards.functions("99.999") == []
    assert all(s.source.startswith("http") for s in profstandards.standards())


def test_hh_synonym_rule() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts" / "build_skill_refs.py"
    spec = importlib.util.spec_from_file_location("build_skill_refs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    got = mod.pick_synonyms(
        "Excel",
        ["Excel", "ms excel"],
        ["MS Excel 2016", "Excel VBA", "Microsoft Excel"],
        lambda s: None,
    )
    assert got == ["MS Excel 2016", "Microsoft Excel"]
    assert mod.pick_synonyms("Java", ["Java"], ["JavaScript"], lambda s: None) == []
    assert mod.pick_synonyms("Vue", ["Vue"], ["VueJS"], skills.canonical) == []
    assert mod.pick_synonyms("SQL", ["SQL"], ["Ms SQL 2000"], skills.canonical) == []
    assert mod.pick_synonyms("PHP", ["PHP"], ["PHP5"], {"PHP": "PHP"}.get) == ["PHP5"]
