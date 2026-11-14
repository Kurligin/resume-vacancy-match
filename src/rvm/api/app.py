"""HTTP API сервиса: загрузки с очередью, вакансии со сверкой резерва, база кандидатов, оценки.

Тяжёлая работа (чтение файлов, модель) идёт в пуле из двух потоков, сверки резерва — по одной
в отдельном потоке; состояние пачек и сверок резерва — в памяти процесса. Без сессии /api/*
отвечает 401 (кроме health и входа), изменяющие запросы — только с заголовком X-Requested-With."""

import hashlib
import io
import logging
import os
import secrets
import threading
import uuid
import zipfile
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from rvm import store
from rvm.api import auth, dashboard, hh, package, triage
from rvm.api.auth import Anyone, Manager, Recruiter
from rvm.ingest import limits, load_bytes, load_text
from rvm.llm import LLMClient
from rvm.llm.fake import DemoFake
from rvm.models import Document, Status
from rvm.pipeline.analyze import Report, ReportStatus, ResumeView, VacancyResult, analyze
from rvm.privacy import mask
from rvm.scoring import PairScore, Verdict
from rvm.store import CandidateStatus

MAX_BATCH = 300  # файлов в одной пачке
MAX_REQUEST_BYTES = 200 * 2**20  # распакованный объём одного запроса на загрузку
OPEN_PATHS = {"/api/health", "/api/login", "/api/login-options"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# скрипты — только свои файлы; inline-стили в разметке интерфейса допустимы
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)
log = logging.getLogger("rvm.api")
STATIC = Path(__file__).parent / "static"
_SKIP_IN_ZIP = ("__MACOSX", ".")  # служебные папки macOS и скрытые файлы
_STATUS = {"from", "to", "package", "names"}  # поля журнала статусов, видимые руководителю

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


class Flow(BaseModel):
    """Поля потока откликов: должность, лучшая вакансия последней версии, ожидание ответа."""

    position: str | None = None  # из фактов резюме
    best_vacancy: str | None = None
    best_title: str | None = None
    best_vacancy_score: float | None = None
    name: str | None = None  # ФИО — только по pii=true и только рекрутеру
    waiting: bool = True  # ответ кандидату ещё не отправлен
    waiting_days: int | None = None
    replied_at: str | None = None


class CandidateItem(Flow):
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


class Card(Flow):
    id: int
    status: CandidateStatus
    created_at: str
    source: str
    possible_duplicate_of: int | None = None
    versions: list[Version]
    evaluations: list[Evaluation]
    events: list[Event]


class StatusChange(BaseModel):
    status: CandidateStatus
    undo: bool = False  # отмена предыдущего решения кнопкой «Отменить»


class RowAnswer(BaseModel):
    status: Status
    note: str = ""
    by: Literal["candidate", "recruiter"] = "candidate"  # ответ кандидата или правка рекрутера


class LoginBody(BaseModel):
    login: str = ""
    password: str = ""
    role: auth.Role | None = None  # демо-вход в один клик


class Decision(BaseModel):
    decision: Literal["invite", "reject"]
    comment: str = ""


class MergeBody(BaseModel):
    other: int  # карточка, которая вливается в текущую


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
    applied: int = 0  # откликов (последние версии резюме)
    waiting: int = 0  # из откликнувшихся не разобрано: new / review
    fit: int = 0  # «подходит» по этой вакансии
    to_customer: int = 0  # у заказчика с этой вакансией


class CandidatePage(BaseModel):
    items: list[CandidateItem]
    total: int  # найдено под фильтрами, без учёта страницы
    limit: int
    offset: int


class VacancyCandidate(BaseModel):
    id: int
    status: CandidateStatus
    resume_id: int
    score: float
    low: float
    high: float
    verdict: Verdict
    blockers: list[str]  # тексты невыполненных обязательных требований
    position: str | None = None
    name: str | None = None  # ФИО — только по pii=true


class Health(BaseModel):
    mode: str
    model: str
    version: str


def _zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    # архивы из Windows-проводника пишут кириллицу в cp866 без флага UTF-8
    return info.filename.encode("cp437").decode("cp866", errors="replace")


def names_sent(card: dict[str, Any]) -> bool:
    """Последний пакет заказчику ушёл с флагом «ФИО в документе»."""
    packs = [e["details"] for e in card["events"] if e["details"].get("package")]
    return bool(packs and packs[-1].get("names"))


def _too_big() -> HTTPException:
    return HTTPException(413, f"в запросе больше {MAX_REQUEST_BYTES // 2**20} МБ")


def _unpack(name: str, data: bytes, budget: int) -> list[tuple[str, bytes]]:
    """Размеры проверяются по ZipInfo.file_size до чтения записей; budget — остаток объёма
    запроса."""
    if not name.lower().endswith(".zip"):
        return [(name, data)]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # у пачки свой бюджет (budget), лимит одного docx к ней не относится
            if sum(i.file_size for i in z.infolist()) > limits.MAX_ZIP_RATIO * max(len(data), 1):
                raise ValueError("архив сжат подозрительно сильно (похоже на zip-бомбу)")
            infos = [
                i
                for i in z.infolist()
                if not i.is_dir()
                and not any(p.startswith(_SKIP_IN_ZIP) for p in _zip_name(i).split("/"))
            ]
            big = next((i for i in infos if i.file_size > limits.MAX_BYTES), None)
            if big is not None:
                raise ValueError(f"{_zip_name(big)} больше {limits.MAX_BYTES} байт")
            if sum(i.file_size for i in infos) > budget:
                raise _too_big()
            return [(_zip_name(i).rsplit("/", 1)[-1], z.read(i)) for i in infos]
    except HTTPException:
        raise
    except Exception as e:
        raise ValueError(f"{name}: архив не прочитан: {e}") from e


def create_app(
    db: Path | None = None, llm: LLMClient | None = None, today: date | None = None
) -> FastAPI:
    """Настройки по умолчанию из окружения: RVM_DB, RVM_TODAY, LLM_* (как у клиента модели),
    RVM_SECRET (подпись сессий), RVM_ALLOWED_HOSTS (домены стенда через запятую),
    RVM_DEMO_LOGIN=1 (вход по роли без пароля), RVM_PASSWORD_<ЛОГИН> (хэши паролей)."""
    env = os.environ
    db = db or Path(env.get("RVM_DB", "var/rvm.sqlite"))
    store.init(db)
    if llm is None:
        mode = env.get("LLM_MODE", "live")
        if mode == "demo" or (mode == "live" and not env.get("LLM_API_KEY")):
            if mode == "live":
                log.warning(
                    "LLM_API_KEY не задан — режим demo: ответы из кэша, новые файлы разбирает "
                    "демо-модель без ИИ"
                )
            llm = LLMClient(DemoFake(), mode="demo")
        elif mode == "live" and not env.get("LLM_MODEL"):
            raise RuntimeError("LLM_API_KEY задан, а LLM_MODEL пуст: укажите id модели в .env")
        else:
            llm = LLMClient()  # неизвестный LLM_MODE — ValueError в клиенте
    if not env.get("RVM_SECRET"):
        log.warning("RVM_SECRET не задан — ключ сессий случайный, вход сбросится при перезапуске")
    secret = env.get("RVM_SECRET", "").encode() or secrets.token_bytes(32)
    demo_login = env.get("RVM_DEMO_LOGIN") == "1"
    if demo_login:
        log.warning("RVM_DEMO_LOGIN=1 — вход по роли без пароля открыт любому")
    hosts = ["localhost", "127.0.0.1"] + [
        h.strip() for h in env.get("RVM_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    llm.purge_expired()
    fixed_today = today or (
        date.fromisoformat(os.environ["RVM_TODAY"]) if os.environ.get("RVM_TODAY") else None
    )
    pool = ThreadPoolExecutor(2, thread_name_prefix="rvm")
    sweeps = ThreadPoolExecutor(1, thread_name_prefix="rvm-reserve")  # сверки по одной
    lock = threading.Lock()
    same_file: dict[str, threading.Lock] = {}  # sha256 → блокировка: один файл — один разбор
    batches: dict[str, list[UploadFileState]] = {}
    reserve: dict[str, dict[str, Any]] = {}

    def now() -> date:
        return fixed_today or date.today()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        pool.shutdown(wait=True, cancel_futures=True)
        sweeps.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        lifespan=lifespan,
        title="Резюме × вакансии",
        version=version("rvm"),
        description="Сопоставление резюме и вакансий: статусы требований с цитатами, балл, "
        "рейтинг, выводы. Модель читает, код решает.",
    )

    async def check(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/api/"):
            if request.method not in SAFE_METHODS and not request.headers.get("x-requested-with"):
                return JSONResponse({"detail": "нужен заголовок X-Requested-With"}, 403)
            request.state.user = auth.read(secret, request.cookies.get(auth.COOKIE))
            if request.state.user is None and path not in OPEN_PATHS:
                return JSONResponse({"detail": "нужен вход"}, 401)
        return await call_next(request)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        response = await check(request, call_next)
        response.headers["Content-Security-Policy"] = CSP
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)  # снаружи guard

    def set_file(f: UploadFileState, **kw: Any) -> None:
        with lock:
            for k, v in kw.items():
                setattr(f, k, v)

    def process(f: UploadFileState, data: bytes, applied: str | None) -> None:
        with lock:
            # shortcut: блокировки не удаляются — по строке на уникальный файл за жизнь процесса
            mine = same_file.setdefault(hashlib.sha256(data).hexdigest(), threading.Lock())
        with mine:
            read(f, data, applied)

    def read(f: UploadFileState, data: bytes, applied: str | None) -> None:
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
                    store.add_evaluation(db, rid, new)
                    if new.vacancies[0].verdict in (Verdict.FIT, Verdict.PARTIAL):
                        state["matches"].append(c["id"])
                state["checked"] += 1
            state["state"] = "done"
        except Exception as e:  # noqa: BLE001 — сверка резерва фоновая, ошибку показываем
            state.update(state="error", error=str(e) or type(e).__name__)

    def flow(user: auth.User, pii: bool, ids: list[int] | None = None) -> dict[int, Flow]:
        out = {}
        for cid, c in store.summaries(db, ids).items():
            created = date.fromisoformat(c["created_at"][:10])
            named = pii if user.role == "recruiter" else names_sent(store.card(db, cid))
            out[cid] = Flow(
                **c,
                name=store.full_name(db, cid) if named else None,
                waiting_days=(now() - created).days if c["waiting"] else None,
            )
        return out

    def batch_ids(batch_id: str) -> list[int] | None:
        with lock:
            files = batches.get(batch_id)
            return None if files is None else [f.candidate_id for f in files if f.candidate_id]

    def start_reserve(vid: str) -> None:
        reserve[vid] = {"state": "running", "checked": 0, "matches": []}
        sweeps.submit(check_reserve, vid)

    def not_found(e: KeyError) -> HTTPException:
        return HTTPException(404, str(e.args[0]) if e.args else "не найдено")

    def visible_card(user: auth.User, candidate_id: int) -> dict[str, Any]:
        """Карточка; руководителю — только кандидаты «у заказчика», без имён файлов без флага ФИО."""
        try:
            c = store.card(db, candidate_id)
        except KeyError as e:
            raise not_found(e) from e
        if user.role == "manager":
            if c["status"] != CandidateStatus.CUST:
                raise HTTPException(404, f"нет кандидата {candidate_id}")
            if not names_sent(c):  # в имени файла часто ФИО
                c["versions"] = [{**v, "name": ""} for v in c["versions"]]
            c["events"] = [for_manager(user, e) for e in c["events"] if e["type"] == "status"]
        return c

    def for_manager(user: auth.User, e: dict[str, Any]) -> dict[str, Any]:
        """Руководителю — смена статуса и пакет; комментарий и решение — только его собственные."""
        mine = e["who"] == user.who
        return {**e, "details": {k: v for k, v in e["details"].items() if mine or k in _STATUS}}

    def shown(user: auth.User, c: dict[str, Any], resume: ResumeView) -> ResumeView:
        """Руководителю — только обезличенные строки; имя файла — из карточки (см. visible_card)."""
        if user.role != "manager":
            return resume
        name = resume.name if names_sent(c) else ""
        return resume.model_copy(update={"original": resume.lines, "name": name})

    # --- вход ---

    @app.post("/api/login", summary="Вход: логин и пароль или демо-вход по роли")
    def login(body: LoginBody, response: Response) -> auth.User:
        if body.role:
            if not demo_login:
                raise HTTPException(403, "демо-вход отключён")
            user = auth.demo_user(body.role)
        else:
            user = auth.check_password(body.login, body.password)
        if user is None:
            raise HTTPException(401, "неверный логин или пароль")
        token = auth.issue(secret, user)
        # shortcut: без Secure — стенд за Caddy и localhost по http; включить, если только https
        response.set_cookie(auth.COOKIE, token, max_age=auth.MAX_AGE, httponly=True, samesite="lax")
        return user

    @app.get("/api/login-options", summary="Показывать ли кнопки демо-входа")
    def login_options() -> dict[str, bool]:
        return {"demo": demo_login}

    @app.post("/api/logout", summary="Выход")
    def logout(response: Response, _: Anyone) -> dict[str, bool]:
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    @app.get("/api/me", summary="Кто вошёл")
    def me(user: Anyone) -> auth.User:
        return user

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
        _: Recruiter,
        applied: Annotated[str | None, Query(description="id вакансии отклика")] = None,
    ) -> BatchCreated:
        items: list[tuple[UploadFileState, bytes]] = []
        used = 0
        for up in files:
            name = up.filename or "файл"
            if (up.size or 0) > MAX_REQUEST_BYTES - used:  # сырой размер — до чтения
                raise _too_big()
            try:
                parts = _unpack(name, up.file.read(), MAX_REQUEST_BYTES - used)
            except ValueError as e:
                items.append((UploadFileState(name=name, state="error", message=str(e)), b""))
                continue
            used += sum(len(d) for _, d in parts)
            if used > MAX_REQUEST_BYTES:
                raise _too_big()
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
    def batch(batch_id: str, _: Recruiter) -> Batch:
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
    def vacancies(_: Anyone) -> list[VacancyItem]:
        return [vacancy_item(v) for v in store.list_vacancies(db)]

    @app.post(
        "/api/vacancies",
        status_code=201,
        summary="Добавить вакансию",
        description="Файл или текст. Запускает фоновую сверку кандидатов «в резерве»: "
        "reserve_matches в списке вакансий — кто подходит хотя бы частично.",
    )
    def add_vacancy(
        _: Recruiter,
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
        start_reserve(vid)
        return vacancy_item(next(v for v in store.list_vacancies(db) if v["id"] == vid))

    @app.post("/api/vacancies/{vacancy_id}/close", summary="Закрыть вакансию")
    def close_vacancy(vacancy_id: str, user: Recruiter) -> dict[str, bool]:
        try:
            store.close_vacancy(db, vacancy_id, who=user.who)
        except KeyError as e:
            raise not_found(e) from e
        return {"ok": True}

    @app.get(
        "/api/vacancies/{vacancy_id}/candidates",
        summary="Рейтинг кандидатов по вакансии",
        description="Оценки последней версии резюме каждого кандидата, лучшие сверху.",
    )
    def vacancy_candidates(
        vacancy_id: str, user: Recruiter, pii: bool = False
    ) -> list[VacancyCandidate]:
        try:
            rows = store.vacancy_candidates(db, vacancy_id)
        except KeyError as e:
            raise not_found(e) from e
        extra = flow(user, pii, [r["id"] for r in rows])
        return [
            VacancyCandidate(**r, position=extra[r["id"]].position, name=extra[r["id"]].name)
            for r in rows
        ]

    # --- кандидаты ---

    @app.get(
        "/api/candidates",
        summary="База кандидатов: поиск, фильтры, сортировка, страницы",
        description="Без page — весь список массивом (как раньше). page=1 — ответ "
        "{items, total, limit, offset}: limit по умолчанию 50, не больше 200.",
    )
    def candidates(
        user: Anyone,
        status: CandidateStatus | None = None,
        q: Annotated[str | None, Query(description="поиск по должности и навыкам")] = None,
        sort: Literal["score", "date"] = "score",
        pii: Annotated[bool, Query(description="ФИО из исходного текста")] = False,
        vacancy: Annotated[str | None, Query(description="отклик или лучшая вакансия")] = None,
        verdict: Annotated[
            str | None, Query(description="через запятую: fit, partial, no_fit, no_data")
        ] = None,
        source: Annotated[str | None, Query(description="источник карточки")] = None,
        days: Annotated[int | None, Query(ge=1, description="пришли за последние N дней")] = None,
        page: Annotated[bool, Query(description="ответ страницей с total")] = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[CandidateItem] | CandidatePage:
        if user.role == "manager":
            status = CandidateStatus.CUST
        try:
            verdicts = [str(Verdict(v.strip())) for v in verdict.split(",")] if verdict else None
        except ValueError as e:
            raise HTTPException(422, f"неизвестный вердикт: {verdict}") from e
        since = (now() - timedelta(days=days)).isoformat() if days else None
        filters: dict[str, Any] = {
            "vacancy": vacancy,
            "verdicts": verdicts,
            "source": source,
            "since": since,
        }
        rows = store.list_candidates(
            db,
            status,
            q,
            sort,
            limit=limit if page else None,
            offset=offset if page else 0,
            **filters,
        )
        extra = flow(user, pii, [c["id"] for c in rows])
        items = [CandidateItem(**c, **extra[c["id"]].model_dump()) for c in rows]
        if not page:  # старый формат: текущий экран ждёт массив
            return items
        total = store.count_candidates(db, status, q, **filters)
        return CandidatePage(items=items, total=total, limit=limit, offset=offset)

    @app.get("/api/candidates/{candidate_id}", summary="Карточка: версии, оценки, история")
    def candidate(candidate_id: int, user: Anyone, pii: bool = False) -> Card:
        c = visible_card(user, candidate_id)
        return Card(**c, **flow(user, pii, [candidate_id])[candidate_id].model_dump())

    @app.patch("/api/candidates/{candidate_id}", summary="Сменить статус кандидата")
    def change_status(candidate_id: int, body: StatusChange, user: Recruiter) -> dict[str, bool]:
        try:
            undo = {"undo": True} if body.undo else {}
            store.set_status(db, candidate_id, body.status, who=user.who, **undo)
        except KeyError as e:
            raise not_found(e) from e
        return {"ok": True}

    @app.delete(
        "/api/candidates/{candidate_id}",
        status_code=204,
        summary="Удалить кандидата со всеми данными",
        description="204 — удалено. 200 с warning — удалено, но журнал базы не обрезан (база "
        "занята): стёртое уйдёт из файла при следующей очистке.",
    )
    def delete(candidate_id: int, user: Recruiter) -> Response:
        warning = ""
        try:
            store.delete_candidate(db, candidate_id, who=user.who)
        except KeyError as e:
            raise not_found(e) from e
        except store.CleanupError as e:
            warning = str(e)
        with lock:  # в пачках — имена файлов (часто ФИО): убираем строки удалённого
            for files in batches.values():
                files[:] = [f for f in files if f.candidate_id != candidate_id]
        if warning:
            return JSONResponse({"ok": True, "warning": warning})
        return Response(status_code=204)

    @app.post(
        "/api/candidates/{candidate_id}/manager-decision",
        summary="Решение руководителя: пригласить на интервью или «не подходит»",
        description="Только для кандидата «у заказчика» (иначе 404); статус → invited / cust_rejected, "
        "комментарий и имя руководителя — в журнале.",
    )
    def manager_decision(candidate_id: int, body: Decision, user: Manager) -> dict[str, bool]:
        visible_card(user, candidate_id)  # руководителю видны только «у заказчика»
        new = (
            CandidateStatus.INVITED if body.decision == "invite" else CandidateStatus.CUST_REJECTED
        )
        store.set_status(
            db, candidate_id, new, who=user.who, decision=body.decision, comment=body.comment
        )
        return {"ok": True}

    @app.post(
        "/api/candidates/{candidate_id}/merge",
        summary="Повтор: объединить карточку other с этой",
    )
    def merge(candidate_id: int, body: MergeBody, user: Recruiter) -> dict[str, bool]:
        try:
            store.merge_candidates(db, candidate_id, body.other, who=user.who)
        except KeyError as e:
            raise not_found(e) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.post(
        "/api/candidates/{candidate_id}/dismiss-duplicate",
        summary="Повтор: это разные люди",
    )
    def dismiss_duplicate(candidate_id: int, user: Recruiter) -> dict[str, bool]:
        try:
            store.dismiss_duplicate(db, candidate_id, who=user.who)
        except KeyError as e:
            raise not_found(e) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

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
    def evaluations(candidate: int, user: Anyone) -> Report:
        c = visible_card(user, candidate)
        try:
            rid = c["versions"][0]["id"]
        except IndexError as e:
            raise HTTPException(404, "нет кандидата или резюме") from e
        report = report_of(rid)
        return report.model_copy(update={"resume": shown(user, c, report.resume)})

    @app.get("/api/evaluations/{resume_id}/{vacancy_id}", summary="Отчёт по паре")
    def pair(resume_id: int, vacancy_id: str, user: Anyone) -> Pair:
        c: dict[str, Any] = {}
        if user.role == "manager":
            cards = (
                store.card(db, x["id"]) for x in store.list_candidates(db, CandidateStatus.CUST)
            )
            c = next((x for x in cards if any(v["id"] == resume_id for v in x["versions"])), {})
            if not c:
                raise HTTPException(404, f"нет резюме {resume_id}")
        report = report_of(resume_id)
        result = next((v for v in report.vacancies if v.id == vacancy_id), None)
        if result is None:
            raise HTTPException(404, f"нет оценки по вакансии {vacancy_id}")
        return Pair(resume=shown(user, c, report.resume), result=result)

    @app.patch(
        "/api/evaluations/{resume_id}/{vacancy_id}/rows/{requirement_id}",
        summary="Ответ на вопрос или правка строки",
        description="Строка получает статус с флагом «со слов кандидата» (by=candidate) или "
        "«изменено рекрутером» (by=recruiter); балл, вилка и места вакансий пересчитываются, "
        "событие пишется в журнал. Ответ на проверку D…/SNAPSHOT балл не меняет.",
    )
    def answer(
        resume_id: int, vacancy_id: str, requirement_id: str, body: RowAnswer, user: Recruiter
    ) -> PairScore:
        flag = store.BY_RECRUITER if body.by == "recruiter" else store.FROM_CANDIDATE
        try:
            return store.answer(
                db,
                resume_id,
                vacancy_id,
                requirement_id,
                body.status,
                body.note,
                who=user.who,
                flag=flag,
            )
        except KeyError as e:
            raise not_found(e) from e

    @app.get("/api/health", summary="Режим, модель, версия")
    def health() -> Health:
        return Health(mode=llm.mode, model=llm.model, version=version("rvm"))

    app.include_router(triage.router(db, batch_ids))
    app.include_router(dashboard.router(db, llm.usage_path))
    app.include_router(package.router(db))
    app.include_router(hh.router(db, start_reserve))
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
