"""Выводы, вопросы, юридический фильтр и письма — модель подменяется фейком, сети нет."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rvm.facts import Discrepancy, Period, ResumeFacts
from rvm.legal import lawful, topic
from rvm.llm import LLMClient, LLMError
from rvm.models import Evidence, MatchRow, ReqKind, Requirement, Section, Status
from rvm.pipeline.explain import build_checks, explain
from rvm.pipeline.letters import LetterKind, letters
from rvm.pipeline.questions import questions
from rvm.scoring import score_pair

NET_DOWN = LLMError("сеть недоступна")


class SchemaFake:
    """Ответ по имени схемы: строка JSON или исключение; нет ответа — «не json»."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        name = kwargs["response_format"]["json_schema"]["name"]
        reply = self.replies.get(name, "не json")
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def llm_of(tmp_path: Path, replies: dict[str, Any]) -> LLMClient:
    return LLMClient(
        SchemaFake(replies),
        base_url="http://localhost:1/v1",
        model="fake",
        mode="live",
        cache_path=tmp_path / "c.sqlite",
    )


REQS = [
    Requirement(id="R1", text="Опыт работы с Vue.js", section=Section.MUST, kind=ReqKind.SKILL),
    Requirement(id="R2", text="Знание Tailwind CSS", section=Section.MUST, kind=ReqKind.SKILL),
    Requirement(id="R3", text="Знание TypeScript", section=Section.MUST, kind=ReqKind.SKILL),
    Requirement(
        id="R4", text="Опыт разработки от 3 лет", section=Section.MUST, kind=ReqKind.EXPERIENCE
    ),
    Requirement(id="R5", text="Опыт с Nuxt", section=Section.NICE, kind=ReqKind.SKILL),
    Requirement(id="R6", text="График 5/2", section=Section.CONDITION, kind=ReqKind.CONDITION),
]
ROWS = [
    MatchRow(
        requirement_id="R1",
        status=Status.NO,
        evidence=[Evidence(line=7, quote="с Vue не работал")],
    ),
    MatchRow(
        requirement_id="R2",
        status=Status.NO,
        evidence=[Evidence(line=8, quote="Tailwind не использовал")],
    ),
    MatchRow(
        requirement_id="R3",
        status=Status.FULL,
        evidence=[Evidence(line=5, quote="TypeScript, React — 4 года")],
    ),
    MatchRow(
        requirement_id="R4",
        status=Status.PARTIAL,
        evidence=[Evidence(line=3, quote="Опыт работы 2 года")],
    ),
    MatchRow(requirement_id="R5", status=Status.UNKNOWN),
    MatchRow(requirement_id="R6", status=Status.UNKNOWN),
]
FACTS = ResumeFacts(
    periods=[
        Period(
            line_from=3,
            line_to=4,
            dates_raw="март 2023 — настоящее время",
            current=True,
            declared_raw="(2 года)",
            declared_months=24,
        )
    ]
)
CHECKS = build_checks(
    FACTS,
    [Discrepancy(period=0, declared=24, computed=44)],
    "резюме, похоже, от февраля 2025",
)
SCORE = score_pair(REQS, ROWS)


def run_explain(tmp_path: Path, reply: Any) -> Any:
    llm = llm_of(tmp_path, {"_Explain": reply})
    return explain(REQS, ROWS, SCORE, CHECKS, 44, None, llm)


def test_guard_drops_invented_skill_and_missing_refs(tmp_path: Path) -> None:
    reply = {
        "strengths": [
            {"text": "Уверенно владеет TypeScript", "refs": ["R3"]},
            {"text": "Большой опыт с Kubernetes", "refs": ["R3"]},
            {"text": "Сильный инженер", "refs": []},
            {"text": "Знает TypeScript", "refs": ["R99"]},
        ],
        "risks": [
            {"text": "Не работал с Vue", "refs": ["R1"]},
            {"text": "Стаж 10 лет не подтверждён", "refs": ["R4"]},
            {"text": "Заявлено 24 мес., по датам 44 мес.", "refs": ["D1"]},
        ],
        "recommendation": {"text": "Предложить вакансию с React", "refs": ["VERDICT"]},
    }
    ins = run_explain(tmp_path, reply)
    assert [p.text for p in ins.strengths] == ["Уверенно владеет TypeScript"]
    assert [p.text for p in ins.risks] == [
        "Не работал с Vue",
        "Заявлено 24 мес., по датам 44 мес.",
    ]
    # React нет в строках VERDICT — рекомендация заменена шаблоном
    assert "React" not in ins.recommendation.text
    assert ins.recommendation.refs
    assert ins.dropped == 5


def test_model_failure_gives_nonempty_fallback(tmp_path: Path) -> None:
    ins = run_explain(tmp_path, NET_DOWN)
    assert ins.fallback
    assert any("TypeScript" in p.text for p in ins.strengths)
    risks = " ".join(p.text for p in ins.risks)
    assert "Vue" in risks and "Tailwind" in risks and "24" in risks
    assert ins.recommendation.text
    assert all(p.refs for p in ins.strengths + ins.risks)


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Планируете ли вы детей?", True),
        ("Сколько вам лет?", True),
        ("Есть ли у вас судимость?", True),
        ("Состоите ли вы в браке?", True),
        ("Какое у вас вероисповедание?", True),
        ("Готовы ли работать по графику 5/2?", False),
        ("Расскажите о карьерном росте и опыте с Vue за 3 года", False),
    ],
)
def test_legal_filter(text: str, bad: bool) -> None:
    assert (topic(text) is not None) is bad


def test_lawful_counts_dropped() -> None:
    kept, dropped = lawful(["Сколько вам лет?", "Готовы ли работать по графику 5/2?"])
    assert kept == ["Готовы ли работать по графику 5/2?"] and dropped == 1


def test_questions_have_requirement_ids_and_pass_filter(tmp_path: Path) -> None:
    reply = {
        "items": [
            {"topic": "Q1", "question": "Планируете ли вы детей?"},
            {"topic": "Q2", "question": "Сколько у вас опыта коммерческой разработки?"},
            {"topic": "Q9", "question": "Лишний вопрос"},
        ]
    }
    llm = llm_of(tmp_path, {"_Questions": reply})
    qs = questions(REQS, ROWS, CHECKS, llm)
    assert qs.dropped == 1
    assert qs.items and len(qs.items) <= 6
    ids = {r.id for r in REQS}
    assert all(q.requirement_id in ids for q in qs.items)
    assert all(topic(q.text) is None for q in qs.items)
    assert "Лишний вопрос" not in [q.text for q in qs.items]
    # must раньше nice: R4 (must, partial) идёт до R5 (nice, unknown)
    order = [q.requirement_id for q in qs.items]
    assert order.index("R4") < order.index("R5")
    texts = [q.text for q in qs.items]
    assert "Сколько у вас опыта коммерческой разработки?" in texts
    assert any("февраля 2025" in t for t in texts)  # несостыковка: дата резюме, шаблон


def test_questions_fallback_on_model_error(tmp_path: Path) -> None:
    qs = questions(REQS, ROWS, CHECKS, llm_of(tmp_path, {"_Questions": NET_DOWN}))
    assert qs.items and all(q.requirement_id for q in qs.items)


def test_reject_letter_names_blockers_only() -> None:
    out = {lt.kind: lt for lt in letters("Frontend-разработчик", REQS, ROWS, SCORE, None)}
    reject = out[LetterKind.REJECT].text
    assert "Vue" in reject and "Tailwind" in reject
    assert "TypeScript" not in reject  # не блокер
    assert topic(reject) is None
    assert LetterKind.OFFER_OTHER not in out
    assert sum(lt.suggested for lt in out.values()) <= 1


def test_offer_other_when_redirected() -> None:
    out = {lt.kind: lt for lt in letters("Frontend", REQS, ROWS, SCORE, "Backend-разработчик")}
    assert out[LetterKind.OFFER_OTHER].suggested
    assert "Backend-разработчик" in out[LetterKind.OFFER_OTHER].text


def test_letters_drop_unlawful_reasons() -> None:
    reqs = [
        Requirement(id="R1", text="Возраст до 35 лет", section=Section.MUST, kind=ReqKind.QUALITY),
        Requirement(id="R2", text="Знание Vue", section=Section.MUST, kind=ReqKind.SKILL),
    ]
    rows = [
        MatchRow(requirement_id="R1", status=Status.NO, evidence=[Evidence(line=1, quote="x")]),
        MatchRow(requirement_id="R2", status=Status.NO, evidence=[Evidence(line=2, quote="y")]),
    ]
    out = {lt.kind: lt for lt in letters("Frontend", reqs, rows, score_pair(reqs, rows), None)}
    assert "Возраст" not in out[LetterKind.REJECT].text
    assert "Vue" in out[LetterKind.REJECT].text


def test_questions_drop_invented_skill(tmp_path: Path) -> None:
    reply = {
        "items": [
            {"topic": "Q1", "question": "Расскажите про опыт с Kubernetes и Go?"},
            {"topic": "Q2", "question": "Сколько у вас опыта коммерческой разработки?"},
            {"topic": "Q3", "question": "Как давно вы работаете с Nuxt — больше 7 лет?"},
        ]
    }
    qs = questions(REQS, ROWS, CHECKS, llm_of(tmp_path, {"_Questions": reply}))
    texts = [q.text for q in qs.items]
    assert not any("Kubernetes" in t or "7 лет" in t for t in texts)
    assert "Сколько у вас опыта коммерческой разработки?" in texts
