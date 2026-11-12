"""Конвейер целиком: одно резюме × набор вакансий → Report. Им пользуются API, интерфейс,
замер качества и живой прогон. Перед любым вызовом модели текст обезличивается."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import cache
from typing import Any
from urllib.parse import urlparse

import openai
from pydantic import BaseModel, Field

from rvm.experience import check_declared, snapshot_note, total_months
from rvm.facts import Discrepancy, ResumeFacts
from rvm.llm import LLMClient, LLMError, LLMTruncated
from rvm.models import Document, FileStatus, Line, MatchRow, Requirement, Status
from rvm.pipeline import explain as explain_mod
from rvm.pipeline import extract_resume, extract_vacancy
from rvm.pipeline import match as match_mod
from rvm.pipeline import questions as questions_mod
from rvm.pipeline.explain import Insights, build_checks, explain
from rvm.pipeline.letters import Letter, letters
from rvm.pipeline.questions import Questions, questions
from rvm.pipeline.verify import LETTER_BLOCK
from rvm.privacy import PrivacyError, assert_clean, mask
from rvm.refs.skills import canonical, entries
from rvm.scoring import (
    Contribution,
    PairScore,
    Place,
    Redirect,
    Verdict,
    rank_vacancies,
    score_pair,
)

WISH_BLOCK = "Желаемая должность"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "ollama"}  # модель не покидает машину
_MODEL_ERRORS = (LLMError, openai.OpenAIError)
_WORD = re.compile(r"[^\W\d_]{3,}")


class ReportStatus(StrEnum):
    OK = "ok"
    PRIVACY_ERROR = "privacy_error"  # ошибка обезличивания: модель не вызывалась


class ResumeView(BaseModel):
    name: str
    lines: list[Line]  # обезличенный текст — то, что видела модель
    original: list[Line]  # исходные строки с теми же номерами — только для показа рекрутеру
    status: FileStatus


class VacancyResult(BaseModel):
    id: str
    title: str
    requirements: list[Requirement]
    rows: list[MatchRow]
    score: PairScore
    verdict: Verdict
    place: int | None
    why_not_first: list[Contribution] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    insights: Insights | None = None
    questions: Questions | None = None
    letters: list[Letter] = Field(default_factory=list)
    clarified: dict[str, str] = Field(default_factory=dict)  # id проверки → пометка рекрутера


class Facts(BaseModel):
    resume: ResumeFacts
    total_months: int
    discrepancies: list[Discrepancy]
    snapshot_note: str | None


class Report(BaseModel):
    status: ReportStatus
    error: str | None = None
    resume: ResumeView
    applied: str | None  # вакансия отклика
    applied_guessed: bool  # True — найдена эвристикой, а не передана
    vacancies: list[VacancyResult]  # в порядке рейтинга
    redirect: Redirect | None
    none_fit: bool
    facts: Facts | None
    warnings: list[str]  # предупреждения файлов и конвейера
    model: str
    prompt_versions: dict[str, str]
    created_at: datetime
    elapsed_s: float


PROMPT_VERSIONS = {
    "extract_vacancy": extract_vacancy.PROMPT_VERSION,
    "extract_resume": extract_resume.PROMPT_VERSION,
    "match": match_mod.PROMPT_VERSION,
    "explain": explain_mod.PROMPT_VERSION,
    "questions": questions_mod.PROMPT_VERSION,
}


def is_local(llm: LLMClient) -> bool:
    return (urlparse(llm.base_url).hostname or "") in LOCAL_HOSTS


def skill_variants(term: str) -> list[str]:
    """Навык → варианты написания по справочнику; нет в справочнике — сам термин."""
    name = canonical(term)
    entry = next((e for e in entries() if e.name == name), None)
    return list(entry.synonyms) if entry else [term]


@cache
def _morph() -> Any:
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def _lemmas(text: str) -> set[str]:
    words = _WORD.findall(text.lower().replace("ё", "е"))
    return {_morph().parse(w)[0].normal_form.replace("ё", "е") for w in words}


def title_of(doc: Document) -> str:
    return next((ln.text.strip() for ln in doc.lines if ln.text.strip()), doc.name)


def guess_applied(resume: Document, vacancies: dict[str, Document]) -> str | None:
    """Вакансия отклика: больше всего значимых слов названия в письме или желаемой должности.
    Нужно не меньше двух слов (или все, если слово одно) и единственный лидер."""
    text = " ".join(ln.text for ln in resume.lines if ln.block in (LETTER_BLOCK, WISH_BLOCK))
    if not text:
        return None
    have = _lemmas(text)
    hits: list[tuple[int, str]] = []
    for vid, doc in vacancies.items():
        words = _lemmas(title_of(doc))
        n = len(words & have)
        if words and n >= min(2, len(words)):
            hits.append((n, vid))
    hits.sort(reverse=True)
    if not hits or (len(hits) > 1 and hits[0][0] == hits[1][0]):
        return None
    return hits[0][1]


def _why(err: Exception) -> str:
    # текст обрезки безопасен и подсказывает, что делать; прочие тексты могут нести ответ модели
    return str(err) if isinstance(err, LLMTruncated) else type(err).__name__


def _unknown_rows(reqs: list[Requirement]) -> list[MatchRow]:
    return [
        MatchRow(
            requirement_id=r.id, status=Status.UNKNOWN, flags=["llm_error"], note="ошибка модели"
        )
        for r in reqs
    ]


def analyze(
    resume: Document,
    vacancies: dict[str, Document],
    llm: LLMClient,
    today: date,
    applied: str | None = None,
    source_name: str | None = None,
) -> Report:
    t0 = time.time()
    warnings = [f"{resume.name}: {w}" for w in resume.warnings]
    applied_guessed = applied is None
    if applied is None:
        applied = guess_applied(resume, vacancies)

    masked_resume = mask(resume, source_name)
    masked_vacs = {vid: mask(doc, names_from_file=False) for vid, doc in vacancies.items()}
    view = ResumeView(
        name=resume.name,
        lines=masked_resume.doc.lines,
        original=resume.lines,
        status=resume.status,
    )
    base = {
        "resume": view,
        "applied": applied,
        "applied_guessed": applied_guessed,
        "model": llm.model,
        "prompt_versions": PROMPT_VERSIONS,
        "created_at": datetime.now(UTC),
    }
    try:
        assert_clean(masked_resume)
    except PrivacyError as err:
        if not is_local(llm):
            return Report(
                status=ReportStatus.PRIVACY_ERROR,
                error=f"ошибка обезличивания: {err}",
                vacancies=[],
                redirect=None,
                none_fit=False,
                facts=None,
                warnings=warnings,
                elapsed_s=round(time.time() - t0, 2),
                **base,
            )
        warnings.append(f"обезличивание неполное ({err}), модель локальная — продолжаем")
    # ложное срабатывание на вакансии не роняет отчёт: пропускаем только эту вакансию
    for vid, mv in list(masked_vacs.items()):
        try:
            assert_clean(mv)
        except PrivacyError as err:
            name = vacancies[vid].name
            if is_local(llm):
                warnings.append(f"{name}: обезличивание неполное ({err}), модель локальная")
                continue
            del masked_vacs[vid]
            warnings.append(
                f"{name}: в тексте вакансии остались контакты, проверьте файл — вакансия пропущена"
            )

    doc = masked_resume.doc
    facts = ResumeFacts()
    if doc.lines:
        try:
            facts = extract_resume.extract_resume_facts(doc, llm)
        except _MODEL_ERRORS as err:
            warnings.append(f"ошибка модели при разборе резюме: {_why(err)}")

    def pair(vid: str) -> VacancyResult:
        mv = masked_vacs[vid]
        vwarn = [f"{vacancies[vid].name}: {w}" for w in vacancies[vid].warnings]
        reqs: list[Requirement] = []
        if mv.doc.lines:
            try:
                reqs = extract_vacancy.extract_requirements(mv.doc, llm)
            except _MODEL_ERRORS as err:
                vwarn.append(f"ошибка модели при разборе вакансии: {_why(err)}")
        rows: list[MatchRow] = []
        if reqs and doc.lines:
            try:
                rows = match_mod.match(reqs, doc, facts, llm, today, skills=skill_variants)
            except _MODEL_ERRORS:  # match сам ловит только LLMError; сеть — здесь
                rows = _unknown_rows(reqs)
        score = score_pair(reqs, rows)
        return VacancyResult(
            id=vid,
            title=title_of(vacancies[vid]),
            requirements=reqs,
            rows=rows,
            score=score,
            verdict=score.verdict,
            place=None,
            warnings=vwarn,
        )

    # пары и выводы независимы: вызовы модели идут параллельно, порядок — как во входе
    workers = int(os.environ.get("RVM_WORKERS", "4"))
    with ThreadPoolExecutor(workers) as pool:
        results = dict(zip(masked_vacs, pool.map(pair, masked_vacs), strict=True))

    ranking = rank_vacancies({vid: r.score for vid, r in results.items()}, applied)
    pair_facts = Facts(
        resume=facts,
        total_months=total_months(facts.periods, today),
        discrepancies=check_declared(facts, today),
        snapshot_note=snapshot_note(facts, today),
    )
    checks = build_checks(facts, pair_facts.discrepancies, pair_facts.snapshot_note)
    rd = ranking.redirect

    def finish(p: Place) -> VacancyResult:
        r = results[p.id]
        r.place, r.why_not_first = p.place, p.why_not_first
        # выводы и письма — по обезличенной таблице пары
        to = rd.to_vacancy if rd and rd.from_vacancy == r.id else None
        to_masked = to and title_of(masked_vacs[to].doc)  # модели — обезличенное название
        r.insights = explain(
            r.requirements, r.rows, r.score, checks, pair_facts.total_months, to_masked, llm
        )
        r.questions = questions(r.requirements, r.rows, checks, llm) if r.rows else None
        r.letters = letters(r.title, r.requirements, r.rows, r.score, to and results[to].title)
        return r

    with ThreadPoolExecutor(workers) as pool:
        ordered = list(pool.map(finish, ranking.places))
    warnings += facts.warnings
    return Report(
        status=ReportStatus.OK,
        vacancies=ordered,
        redirect=ranking.redirect,
        none_fit=ranking.none_fit,
        facts=pair_facts,
        warnings=warnings,
        elapsed_s=round(time.time() - t0, 2),
        **base,
    )
