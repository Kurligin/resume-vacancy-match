"""Пакет для заказчика (spec §6б П3): сравнительная таблица по обязательным требованиям, по каждому
кандидату «Кратко», риски, вопросы на собеседование. DOCX или печатный HTML (PDF — печатью
браузера). Тексты — через юридический фильтр, ФИО — только по решению рекрутера."""

import html
import io
from pathlib import Path
from typing import Annotated, Literal, NamedTuple
from urllib.parse import quote

from docx import Document as Docx
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from rvm import store
from rvm.api.auth import Recruiter
from rvm.legal import lawful
from rvm.models import Requirement, Section
from rvm.pipeline.analyze import VacancyResult
from rvm.store import CandidateStatus

STATUS = {"full": "есть", "partial": "частично", "no": "нет", "unknown": "не указано"}
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_CANDIDATES = 20
CSS = (
    "body{font:14px/1.5 sans-serif;margin:24px}table{border-collapse:collapse}"
    "td,th{border:1px solid #999;padding:4px 8px;text-align:left}section{break-inside:avoid}"
)


class PackageBody(BaseModel):
    vacancy_id: str
    candidate_ids: list[int] = Field(min_length=1, max_length=MAX_CANDIDATES)
    names: bool = False  # ФИО в документе


class Entry(NamedTuple):
    label: str
    v: VacancyResult


class Package(NamedTuple):
    title: str
    musts: list[Requirement]
    entries: list[Entry]


def collect(db: Path, vacancy_id: str, ids: list[int], names: bool) -> Package:
    """Оценки кандидатов по вакансии (последняя версия резюме); нет оценки — KeyError."""
    info = store.summaries(db, ids)
    entries = []
    for cid in ids:
        if cid not in info or info[cid]["resume_id"] is None:
            raise KeyError(f"нет кандидата {cid}")
        rep = store.load_report(db, info[cid]["resume_id"])
        v = next((x for x in rep.vacancies if x.id == vacancy_id), None)
        if v is None:
            raise KeyError(f"нет оценки кандидата {cid} по вакансии {vacancy_id}")
        entries.append(Entry((names and store.full_name(db, cid)) or f"Кандидат №{cid}", v))
    musts = {r.id: r for e in entries for r in e.v.requirements if r.section == Section.MUST}
    return Package(entries[0].v.title, list(musts.values()), entries)


def _cell(v: VacancyResult, rid: str) -> str:
    row = next((r for r in v.rows if r.requirement_id == rid), None)
    if row is None:
        return STATUS["unknown"]
    told = store.FROM_CANDIDATE in row.flags
    return STATUS[row.status] + (f" ({store.FROM_CANDIDATE})" if told else "")


def table(p: Package) -> list[list[str]]:
    rows = [["Требование"] + [e.label for e in p.entries]]
    rows += [[r.text] + [_cell(e.v, r.id) for e in p.entries] for r in p.musts]
    score = [f"{e.v.score.score:g} ({e.v.score.low:g}–{e.v.score.high:g})" for e in p.entries]
    return rows + [["Балл (вилка)"] + score]


def sections(v: VacancyResult) -> list[tuple[str, list[str]]]:
    ins, qs = v.insights, v.questions
    brief = [p.text for p in ins.strengths] + [ins.recommendation.text] if ins else []
    risks = [p.text for p in ins.risks] if ins else []
    asks = [q.text for q in qs.items] if qs else []
    return [
        ("Кратко", lawful(brief)[0]),
        ("Риски", lawful(risks)[0]),
        ("Вопросы на собеседование", lawful(asks)[0]),
    ]


def to_docx(p: Package) -> bytes:
    d = Docx()
    d.add_heading(f"Кандидаты на вакансию «{p.title}»", 0)
    d.add_heading("Обязательные требования", 1)
    rows = table(p)
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    for i, row in enumerate(rows):
        for j, text in enumerate(row):
            t.cell(i, j).text = text
    for e in p.entries:
        d.add_heading(f"{e.label} — балл {e.v.score.score:g}", 1)
        for name, items in sections(e.v):
            d.add_heading(name, 2)
            for text in items or ["—"]:
                d.add_paragraph(text, style="List Bullet" if items else None)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def to_html(p: Package) -> str:
    esc = html.escape
    rows = table(p)
    head = "".join(f"<th>{esc(c)}</th>" for c in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows[1:]
    )
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>",
        f"<title>{esc(p.title)}</title><style>{CSS}</style></head><body>",
        f"<h1>Кандидаты на вакансию «{esc(p.title)}»</h1><h2>Обязательные требования</h2>",
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
    ]
    for e in p.entries:
        parts.append(f"<section><h2>{esc(e.label)} — балл {e.v.score.score:g}</h2>")
        for name, items in sections(e.v):
            lis = "".join(f"<li>{esc(t)}</li>" for t in items) or "<li>—</li>"
            parts.append(f"<h3>{esc(name)}</h3><ul>{lis}</ul>")
        parts.append("</section>")
    return "".join(parts) + "</body></html>"


def _docx_response(p: Package) -> Response:
    name = quote(f"Пакет — {p.title}.docx")
    return Response(
        to_docx(p),
        media_type=DOCX_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


def router(db: Path) -> APIRouter:
    r = APIRouter()

    def build(vacancy_id: str, ids: list[int], names: bool) -> Package:
        try:
            return collect(db, vacancy_id, ids, names)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e

    @r.post(
        "/api/package",
        summary="Пакет для заказчика (DOCX); кандидатам ставится «у заказчика»",
        response_class=Response,
    )
    def make(body: PackageBody, user: Recruiter) -> Response:
        p = build(body.vacancy_id, body.candidate_ids, body.names)
        out = _docx_response(p)
        store.set_statuses(
            db,
            body.candidate_ids,
            CandidateStatus.CUST,
            who=user.who,
            vacancy_id=body.vacancy_id,
            package=True,
        )
        return out

    @r.get(
        "/api/package",
        summary="Тот же пакет без смены статусов: DOCX или печатный HTML (format=html)",
        response_class=Response,
    )
    def show(
        _: Recruiter,
        vacancy_id: str,
        candidate_ids: Annotated[list[int], Query(min_length=1, max_length=MAX_CANDIDATES)],
        names: bool = False,
        format: Literal["docx", "html"] = "docx",
    ) -> Response:
        p = build(vacancy_id, candidate_ids, names)
        if format == "html":
            return Response(to_html(p), media_type="text/html; charset=utf-8")
        return _docx_response(p)

    return r
