from importlib.resources import files

from rvm.pipeline import analyze, extract_resume, extract_vacancy, match


def test_v2_prompts_connected_v1_kept() -> None:
    prompts = files("rvm") / "prompts"
    for mod, name in (
        (extract_vacancy, "extract_vacancy"),
        (match, "match"),
        (extract_resume, "extract_resume"),
    ):
        assert mod.PROMPT_VERSION == f"{name}.v2"
        assert analyze.PROMPT_VERSIONS[name] == f"{name}.v2"
        assert mod.PROMPT.read_text(encoding="utf-8").strip()
        assert (prompts / f"{name}.v1.md").is_file()  # старая версия — для сравнения замеров


def test_v2_rules_present() -> None:
    vacancy = extract_vacancy.PROMPT.read_text(encoding="utf-8")
    assert "одно требование" in vacancy and "одним" in vacancy and "4–6" in vacancy
    assert "коммерческих проектов нет" in match.PROMPT.read_text(encoding="utf-8")
