"""Письма кандидату по шаблонам: текст из таблицы, без модели. Отказ называет только деловые
причины — невыполненные обязательные требования; темы из стоп-листа ТК не попадают никуда."""

from enum import StrEnum

from pydantic import BaseModel

from rvm.legal import lawful
from rvm.models import MatchRow, Requirement, Section, Status
from rvm.scoring import PairScore, Verdict

MAX_REASONS = 3
HELLO = "Здравствуйте!"


class LetterKind(StrEnum):
    INVITE = "invite"  # пригласить
    OFFER_OTHER = "offer_other"  # предложить другую вакансию
    RESERVE = "reserve"  # в резерв
    REJECT = "reject"  # отказ


class Letter(BaseModel):
    kind: LetterKind
    text: str
    suggested: bool  # шаблон по вердикту и переадресации


def _list(texts: list[str]) -> str:
    kept, _ = lawful(texts)
    return "; ".join(t.rstrip(".") for t in kept[:MAX_REASONS])


def letters(
    title: str,
    reqs: list[Requirement],
    rows: list[MatchRow],
    score: PairScore,
    redirect_to: str | None,
) -> list[Letter]:
    req_of = {r.id: r for r in reqs}
    status = {r.requirement_id: r.status for r in rows}
    must = [r for r in reqs if r.section == Section.MUST]
    strong = _list([r.text for r in must if status.get(r.id) == Status.FULL])
    # причины отказа: блокеры; нет блокеров — must с наибольшими потерями балла
    losses = sorted(
        (c for c in score.contributions if c.section == Section.MUST and c.loss > 0),
        key=lambda c: -c.loss,
    )
    reasons = _list([req_of[i].text for i in score.blockers if i in req_of]) or _list(
        [req_of[c.requirement_id].text for c in losses if c.requirement_id in req_of]
    )
    thanks = f"{HELLO} Спасибо за отклик на вакансию «{title}»."
    texts = {
        LetterKind.INVITE: f"{thanks} "
        + (f"Ваш опыт соответствует нашим требованиям: {strong}. " if strong else "")
        + "Приглашаем вас на собеседование — предложите, пожалуйста, удобное время.",
        LetterKind.RESERVE: f"{thanks} Сейчас мы не готовы сделать предложение, но хотели бы "
        "сохранить ваше резюме в резерве и вернуться к вам, когда появится подходящая позиция.",
        LetterKind.REJECT: f"{thanks} К сожалению, сейчас мы не готовы продолжить: "
        + (
            f"в резюме мы не нашли подтверждения обязательных требований — {reasons}."
            if reasons
            else "ваш опыт пока не соответствует требованиям вакансии."
        )
        + " Благодарим за интерес и желаем успехов.",
    }
    if redirect_to:
        texts[LetterKind.OFFER_OTHER] = (
            f"{thanks} "
            + (f"По этой вакансии нам не хватает: {reasons}. " if reasons else "")
            + f"При этом ваш опыт хорошо подходит к вакансии «{redirect_to}». "
            "Готовы рассмотреть вас на неё — ответьте, пожалуйста, если вам интересно."
        )
    suggested = {
        Verdict.FIT: LetterKind.INVITE,
        Verdict.PARTIAL: LetterKind.RESERVE,
        Verdict.NO_FIT: LetterKind.REJECT,
    }.get(score.verdict)
    if redirect_to:
        suggested = LetterKind.OFFER_OTHER
    return [Letter(kind=k, text=t, suggested=k == suggested) for k, t in texts.items()]
