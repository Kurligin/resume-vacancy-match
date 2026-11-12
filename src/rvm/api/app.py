"""HTTP API сервиса: загрузки с очередью, вакансии со сверкой резерва, база кандидатов, оценки.

Тяжёлая работа (чтение файлов, модель) идёт в пуле из двух потоков; состояние пачек и сверок
резерва — в памяти процесса."""

import io
import os
import threading
import uuid
import zipfile
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rvm import store
from rvm.ingest import load_bytes, load_text
from rvm.ingest.readers import check_zip
from rvm.llm import LLMClient
from rvm.models import Document, Status
from rvm.pipeline.analyze import Report, ReportStatus, ResumeView, VacancyResult, analyze
from rvm.privacy import mask
from rvm.scoring import PairScore, Verdict, rank_vacancies
from rvm.store import CandidateStatus

MAX_BATCH = 300  # файлов в одной пачке
STATIC = Path(__file__).parent / "static"
_SKIP_IN_ZIP = ("__MACOSX", ".")  # служебные папки macOS и скрытые файлы

FileState = Literal["queued", "reading", "done", "error"]


class UploadFileState(BaseModel):
    name: str
    state: FileState  # в очереди / распознаём / готово / ошибка
    message: str = ""
    candidate_id: int | None = None


class Batch(BaseModel):
    batch_id: str
    files: list[UploadFileState]


class BatchCreated(BaseModel):
    batch_id: str
    files: int


class CandidateItem(BaseModel):
    id: int
    status: CandidateStatus
    created_at: str
    source: str
    versions: int
    best_score: float | None


class Version(BaseModel):
    id: int
    sha256: str
    name: str
    status: str
    created_at: str


class Evaluation(BaseModel):
    resume_id: int
    vacancy_id: str
    title: str | None
    model: str
    prompt_versions: dict[str, str]
    score: float
    low: float
    high: float
    verdict: Verdict
    place: int | None
    created_at: str


class Event(BaseModel):
    at: str
    type: str
    vacancy_id: str | None
    who: str
    details: dict[str, Any]


class Card(BaseModel):
    id: int
    status: CandidateStatus
    created_at: str
    source: str
    versions: list[Version]
    evaluations: list[Evaluation]
    events: list[Event]


class StatusChange(BaseModel):
    status: CandidateStatus


class RowAnswer(BaseModel):
    status: Status
    note: str = ""


class Pair(BaseModel):
    resume: ResumeView  # с исходным текстом — только для показа рекрутеру
    result: VacancyResult


class VacancyItem(BaseModel):
    id: str
    title: str
    source: str
    is_open: bool
    created_at: str
    reserve_state: Literal["running", "done", "error"] | None = None
    reserve_checked: int = 0  # сколько кандидатов из резерва проверено
    reserve_matches: list[int] = []  # кто из резерва подходит хотя бы частично
    reserve_error: str = ""


class Health(BaseModel):
    mode: str
    model: str
    version: str


def _zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    # архивы из Windows-проводника пишут кириллицу в cp866 без флага UTF-8
    return info.filename.encode("cp437").decode("cp866", errors="replace")


def _unpack(name: str, data: bytes) -> list[tuple[str, bytes]]:
    if not name.lower().endswith(".zip"):
        return [(name, data)]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            check_zip(z)
            return [
                (_zip_name(i).rsplit("/", 1)[-1], z.read(i))
                for i in z.infolist()
                if not i.is_dir()
                and not any(p.startswith(_SKIP_IN_ZIP) for p in _zip_name(i).split("/"))
            ]
    except Exception as e:
        raise ValueError(f"{name}: архив не прочитан: {e}") from e


def _with_vacancy(old: Report, new: Report) -> Report:
    """Старые оценки (с правками рекрутера) + оценка по новой вакансии, места пересчитаны."""
    results = {v.id: v for v in old.vacancies} | {v.id: v for v in new.vacancies}
    ranking = rank_vacancies({vid: v.score for vid, v in results.items()}, old.applied)
    for p in ranking.places:
        results[p.id].place, results[p.id].why_not_first = p.place, p.why_not_first
    ordered = sorted(results.values(), key=lambda v: (v.place is None, v.place))
    return old.model_copy(
        update={"vacancies": ordered, "redirect": ranking.redirect, "none_fit": ranking.none_fit}
    )


def create_app(
    db: Path | None = None, llm: LLMClient | None = None, today: date | None = None
) -> FastAPI:
    """Настройки по умолчанию из окружения: RVM_DB, RVM_TODAY, LLM_* (как у клиента модели)."""
    db = db or Path(os.environ.get("RVM_DB", "var/rvm.sqlite"))
    store.init(db)
    llm = llm or LLMClient()
    llm.purge_expired()
    fixed_today = today or (
        date.fromisoformat(os.environ["RVM_TODAY"]) if os.environ.get("RVM_TODAY") else None
    )
    pool = ThreadPoolExecutor(2, thread_name_prefix="rvm")
    lock = threading.Lock()
    batches: dict[str, list[UploadFileState]] = {}
    reserve: dict[str, dict[str, Any]] = {}

    def now() -> date:
        return fixed_today or date.today()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        lifespan=lifespan,
        title="Резюме × вакансии",
        version=version("rvm"),
        description="Сопоставление резюме и вакансий: статусы требований с цитатами, балл, "
        "рейтинг, выводы. Модель читает, код решает.",
    )

    def set_file(f: UploadFileState, **kw: Any) -> None:
        with lock:
            for k, v in kw.items():
                setattr(f, k, v)

    def process(f: UploadFileState, data: bytes, applied: str | None) -> None:
        set_file(f, state="reading")
        try:
            doc = load_bytes(f.name, data)
            if not doc.lines:
                raise ValueError("; ".join(doc.warnings) or f"файл не прочитан: {doc.status}")
            added = store.add_resume(db, data, doc, mask(doc, f.name))
            set_file(f, candidate_id=added.candidate_id)
            if not added.new:
                try:
                    store.load_report(db, added.resume_id)
                    set_file(f, state="done", message="этот файл уже загружен, оценки из базы")
                    return
                except KeyError:
                    pass  # прошлый разбор не завершился — оцениваем заново
            report = analyze(
                doc, store.open_vacancies(db), llm, now(), applied=applied, source_name=f.name
            )
            store.save_report(db, added.resume_id, report)
            if report.status != ReportStatus.OK:
                raise ValueError(report.error or "оценка не выполнена")
            set_file(f, state="done", message="; ".join(report.warnings))
        except Exception as e:  # noqa: BLE001 — ошибка одного файла не останавливает пачку
            set_file(f, state="error", message=str(e) or type(e).__name__)

    def check_reserve(vid: str) -> None:
        state = reserve[vid]
        try:
            vac = {vid: store.open_vacancies(db)[vid]}
            for c in store.list_candidates(db, status=CandidateStatus.RESERVE):
                rid = store.card(db, c["id"])["versions"][0]["id"]
                try:
                    old = store.load_report(db, rid)
                except KeyError:
                    continue  # разбор резюме ещё не завершён — сверим при следующей вакансии
                r = old.resume
                doc = Document(name=r.name, lines=r.original, status=r.status)
                new = analyze(doc, vac, llm, now(), applied=old.applied, source_name=r.name)
                if new.status == ReportStatus.OK and new.vacancies:
                    store.save_report(db, rid, _with_vacancy(old, new))
                    if new.vacancies[0].verdict in (Verdict.FIT, Verdict.PARTIAL):
                        state["matches"].append(c["id"])
                state["checked"] += 1
            state["state"] = "done"
        except Exception as e:  # noqa: BLE001 — сверка резерва фоновая, ошибку показываем
            state.update(state="error", error=str(e) or type(e).__name__)

    def not_found(e: KeyError) -> HTTPException:
        return HTTPException(404, str(e.args[0]) if e.args else "не найдено")

    # --- загрузки ---

    @app.post(
        "/api/uploads",
        status_code=202,
        summary="Загрузка резюме",
        description=f"Один или несколько файлов, zip-архивы раскрываются (не больше {MAX_BATCH} "
        "файлов в пачке). Файлы встают в очередь: чтение → база → оценка по открытым "
        "вакансиям. applied — id вакансии, на которую откликнулись.",
    )
    def upload(
        files: Annotated[list[UploadFile], File(description="резюме или zip")],
        applied: Annotated[str | None, Query(description="id вакансии отклика")] = None,
    ) -> BatchCreated:
        items: list[tuple[UploadFileState, bytes]] = []
        for up in files:
            name = up.filename or "файл"
            try:
                parts = _unpack(name, up.file.read())
            except ValueError as e:
                items.append((UploadFileState(name=name, state="error", message=str(e)), b""))
                continue
            items += [(UploadFileState(name=n, state="queued"), d) for n, d in parts]
            if len(items) > MAX_BATCH:
                raise HTTPException(413, f"в пачке больше {MAX_BATCH} файлов")
        bid = uuid.uuid4().hex
        with lock:
            batches[bid] = [f for f, _ in items]
        for f, data in items:
            if f.state == "queued":
                pool.submit(process, f, data, applied)
        return BatchCreated(batch_id=bid, files=len(items))

    @app.get("/api/uploads/{batch_id}", summary="Прогресс пачки по файлам")
    def batch(batch_id: str) -> Batch:
        with lock:
            if batch_id not in batches:
                raise HTTPException(404, "нет такой пачки")
            return Batch(batch_id=batch_id, files=[f.model_copy() for f in batches[batch_id]])

    # --- вакансии ---

    def vacancy_item(v: dict[str, Any]) -> VacancyItem:
        r = reserve.get(v["id"])
        if r is None:
            return VacancyItem(**v)
        return VacancyItem(
            **v,
            reserve_state=r["state"],
            reserve_checked=r["checked"],
            reserve_matches=list(r["matches"]),
            reserve_error=r.get("error", ""),
        )

    @app.get("/api/vacancies", summary="Список вакансий с итогом сверки резерва")
    def vacancies() -> list[VacancyItem]:
        return [vacancy_item(v) for v in store.list_vacancies(db)]

    @app.post(
        "/api/vacancies",
        status_code=201,
        summary="Добавить вакансию",
        description="Файл или текст. Запускает фоновую сверку кандидатов «в резерве»: "
        "reserve_matches в списке вакансий — кто подходит хотя бы частично.",
    )
    def add_vacancy(
        file: Annotated[UploadFile | None, File(description="файл вакансии")] = None,
        text: Annotated[str | None, Form(description="текст вакансии")] = None,
    ) -> VacancyItem:
        if file is not None:
            doc, source = load_bytes(file.filename or "вакансия", file.file.read()), "файл"
        elif text and text.strip():
            doc, source = load_text("вакансия", text), "текст"
        else:
            raise HTTPException(422, "нужен файл или текст вакансии")
        if not doc.lines:
            raise HTTPException(422, "; ".join(doc.warnings) or "вакансия не прочитана")
        vid = store.add_vacancy(db, doc, source=source)
        reserve[vid] = {"state": "running", "checked": 0, "matches": []}
        pool.submit(check_reserve, vid)
        return vacancy_item(next(v for v in store.list_vacancies(db) if v["id"] == vid))

    @app.post("/api/vacancies/{vacancy_id}/close", summary="Закрыть вакансию")
    def close_vacancy(vacancy_id: str) -> dict[str, bool]:
        try:
            store.close_vacancy(db, vacancy_id)
        except KeyError as e:
            raise not_found(e) from e
        return {"ok": True}

    # --- кандидаты ---

    @app.get("/api/candidates", summary="База кандидатов: поиск, фильтр по статусу, сортировка")
    def candidates(
        status: CandidateStatus | None = None,
        q: Annotated[str | None, Query(description="поиск по должности и навыкам")] = None,
        sort: Literal["score", "date"] = "score",
    ) -> list[CandidateItem]:
        return [CandidateItem(**c) for c in store.list_candidates(db, status, q, sort)]

    @app.get("/api/candidates/{candidate_id}", summary="Карточка: версии, оценки, история")
    def candidate(candidate_id: int) -> Card:
        try:
            return Card(**store.card(db, candidate_id))
        except KeyError as e:
            raise not_found(e) from e

    @app.patch("/api/candidates/{candidate_id}", summary="Сменить статус кандидата")
    def change_status(candidate_id: int, body: StatusChange) -> dict[str, bool]:
        try:
            store.set_status(db, candidate_id, body.status)
        except KeyError as e:
            raise not_found(e) from e
        return {"ok": True}

    @app.delete(
        "/api/candidates/{candidate_id}",
        status_code=204,
        summary="Удалить кандидата со всеми данными",
    )
    def delete(candidate_id: int) -> Response:
        try:
            store.delete_candidate(db, candidate_id)
        except KeyError as e:
            raise not_found(e) from e
        return Response(status_code=204)

    # --- оценки ---

    def report_of(resume_id: int) -> Report:
        try:
            return store.load_report(db, resume_id)
        except KeyError as e:
            raise not_found(e) from e

    @app.get(
        "/api/evaluations",
        summary="Рейтинг вакансий для кандидата",
        description="Отчёт по последней версии резюме: вакансии в порядке рейтинга, "
        "переадресация, факты, исходный текст для показа.",
    )
    def evaluations(candidate: int) -> Report:
        try:
            rid = store.card(db, candidate)["versions"][0]["id"]
        except (KeyError, IndexError) as e:
            raise HTTPException(404, "нет кандидата или резюме") from e
        return report_of(rid)

    @app.get("/api/evaluations/{resume_id}/{vacancy_id}", summary="Отчёт по паре")
    def pair(resume_id: int, vacancy_id: str) -> Pair:
        report = report_of(resume_id)
        result = next((v for v in report.vacancies if v.id == vacancy_id), None)
        if result is None:
            raise HTTPException(404, f"нет оценки по вакансии {vacancy_id}")
        return Pair(resume=report.resume, result=result)

    @app.patch(
        "/api/evaluations/{resume_id}/{vacancy_id}/rows/{requirement_id}",
        summary="Ответ на вопрос или правка строки",
        description="Строка получает статус с флагом «со слов кандидата»; балл, вилка и места "
        "вакансий пересчитываются, событие пишется в журнал.",
    )
    def answer(resume_id: int, vacancy_id: str, requirement_id: str, body: RowAnswer) -> PairScore:
        try:
            return store.answer(db, resume_id, vacancy_id, requirement_id, body.status, body.note)
        except KeyError as e:
            raise not_found(e) from e

    @app.get("/api/health", summary="Режим, модель, версия")
    def health() -> Health:
        return Health(mode=llm.mode, model=llm.model, version=version("rvm"))

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
