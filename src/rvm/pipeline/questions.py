"""Вопросы кандидату: темы выбирает код (unknown/partial по must и nice, несостыковки), модель
только формулирует. У вопроса — requirement_id: ответ кандидата пересчитывает эту строку."""

from importlib.resources import files

from pydantic import BaseModel

from rvm.legal import topic
from rvm.llm import LLMClient
from rvm.models import MatchRow, ReqKind, Requirement, Section, Status
from rvm.pipeline.explain import MODEL_ERRORS, Check
from rvm.scoring import config

PROMPT_VERSION = "questions.v1"
PROMPT = files("rvm") / "prompts" / f"{PROMPT_VERSION}.md"
MAX_QUESTIONS = 6
ASKED = (Section.MUST, Section.NICE)


class Question(BaseModel):
    requirement_id: str  # id требования; для несостыковки без требования к стажу — id проверки
    text: str


class Questions(BaseModel):
    items: list[Question]
    dropped: int = 0  # отброшено юридическим фильтром


class _Topic(BaseModel):
    key: str
    requirement_id: str
    about: str
    template: str
    weight: float


class _Item(BaseModel):
    topic: str
    question: str


class _Questions(BaseModel):
    items: list[_Item]


def _topics(reqs: list[Requirement], rows: list[MatchRow], checks: list[Check]) -> list[_Topic]:
    status = {r.requirement_id: r.status for r in rows}
    found: list[tuple[str, str, str, float]] = []  # requirement_id, about, template, weight
    for r in reqs:
        s = status.get(r.id, Status.UNKNOWN)
        if r.section not in ASKED or s not in (Status.UNKNOWN, Status.PARTIAL):
            continue
        if s == Status.UNKNOWN:
            about, tpl = "в резюме нет данных", f"Расскажите, пожалуйста, о вашем опыте: {r.text}."
        else:
            about = "в резюме выполнено частично"
            tpl = f"Уточните, пожалуйста, ваш опыт: {r.text}. В резюме это видно не полностью."
        found.append((r.id, f"{r.text} — {about}", tpl, config.WEIGHTS[r.section]))
    exp = next((r for r in reqs if r.kind == ReqKind.EXPERIENCE and r.section in ASKED), None)
    for c in checks:
        rid = exp.id if exp else c.id
        weight = config.WEIGHTS[exp.section] if exp else config.WEIGHTS[Section.MUST]
        found.append((rid, f"несостыковка: {c.text}", c.ask, weight))
    found.sort(key=lambda t: -t[3])  # sorted устойчив: внутри веса — порядок таблицы
    return [
        _Topic(key=f"Q{i}", requirement_id=rid, about=about, template=tpl, weight=w)
        for i, (rid, about, tpl, w) in enumerate(found, 1)
    ]


def questions(
    reqs: list[Requirement], rows: list[MatchRow], checks: list[Check], llm: LLMClient
) -> Questions:
    topics = _topics(reqs, rows, checks)
    if not topics:
        return Questions(items=[])
    worded: dict[str, str] = {}
    messages = [
        {"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": "\n".join(f"{t.key}: {t.about}" for t in topics)},
    ]
    try:
        answer: _Questions = llm.complete(messages, _Questions, PROMPT_VERSION)
        worded = {i.topic: i.question.strip() for i in answer.items if i.question.strip()}
    except MODEL_ERRORS:
        pass  # формулировки по шаблону
    items = [
        Question(requirement_id=t.requirement_id, text=worded.get(t.key, t.template))
        for t in topics
    ]
    kept = [q for q in items if topic(q.text) is None]
    return Questions(items=kept[:MAX_QUESTIONS], dropped=len(items) - len(kept))
