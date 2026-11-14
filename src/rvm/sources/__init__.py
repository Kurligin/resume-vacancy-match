"""Источники откликов («Входящие»): почта, hh, папка.

Источник — функция fetch (см. base.Fetch), возвращающая новые элементы. Каждый элемент идёт в тот
же конвейер, что и ручная загрузка (take из api.app: распаковка zip, лимиты, очередь разбора,
пачка в /api/uploads/{id}); ref элемента записывается в source_seen и повторно не забирается.
Новый источник — модуль с fetch и строка в REGISTRY.
"""

import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from rvm import store
from rvm.sources import folder, hh, mail
from rvm.sources.base import Fetch, Fetched, Item, Source

__all__ = ["REGISTRY", "Fetch", "Fetched", "Inbox", "Item", "Source"]

REGISTRY = [
    Source(
        "mail",
        "Почта",
        "почта",
        ("RVM_IMAP_HOST", "RVM_IMAP_USER", "RVM_IMAP_PASSWORD"),
        mail.fetch,
        "RVM_IMAP_USER",
    ),
    Source("hh", "hh.ru", "hh", ("RVM_HH_TOKEN",), hh.fetch),
    Source("folder", "Папка", "папка", ("RVM_INBOX_DIR",), folder.fetch),
]
RECENT = 20
log = logging.getLogger("rvm.sources")

# (элементы с общим ref, значение candidates.source) → id пачки и задачи разбора её файлов
Take = Callable[[list[Item], str], tuple[str, list[Future[Any]]]]


def _on_all(futures: list[Future[Any]], then: Callable[[], None]) -> None:
    """then() — когда завершатся все задачи (сразу, если задач нет)."""
    left, lock = [len(futures)], threading.Lock()

    def one(_: Future[Any]) -> None:
        with lock:
            left[0] -= 1
            last = left[0] == 0
        if last:
            then()

    if not futures:
        then()
    for f in futures:
        f.add_done_callback(one)


class Inbox:
    """Опрос источников и их состояние в памяти: когда проверяли, ошибка, сколько за сегодня,
    последние 20 элементов. files(batch_id) — копия файлов пачки или None."""

    def __init__(
        self,
        db: Path,
        take: Take,
        files: Callable[[str], list[Any] | None],
        today: Callable[[], date],
        env: Mapping[str, str] = os.environ,
        sources: list[Source] = REGISTRY,
    ) -> None:
        self.db, self.take, self.files, self.today, self.env = db, take, files, today, env
        self.sources = {s.name: s for s in sources}
        self.locks = {s.name: threading.Lock() for s in sources}
        self.state: dict[str, dict[str, Any]] = {
            s.name: {
                "last_check": None,
                "last_error": "",
                "day": None,
                "today_items": 0,
                "today_skipped": 0,
                "recent": deque(maxlen=RECENT),
            }
            for s in sources
        }
        self.stop = threading.Event()
        self.minutes = 0.0  # период фонового опроса; 0 — опрос не запущен

    def missing(self, name: str) -> list[str]:
        return [k for k in self.sources[name].needs if not self.env.get(k)]

    def configured(self) -> list[str]:
        return [n for n in self.sources if not self.missing(n)]

    def running(self, name: str) -> bool:
        return self.locks[name].locked()

    def start(self, minutes: float) -> None:
        """Фоновый опрос настроенных источников: сразу и раз в minutes."""
        self.minutes = minutes

        def loop() -> None:
            while True:
                for name in self.configured():
                    try:
                        self.check(name)
                    except Exception:  # база занята и т. п.: поток опроса живёт
                        log.exception("источник %s: опрос не завершён", name)
                if self.stop.wait(minutes * 60):
                    return

        threading.Thread(target=loop, name="rvm-sources", daemon=True).start()

    def check(self, name: str) -> bool:
        """Опросить источник сейчас (синхронно). False — уже идёт опрос."""
        if not self.locks[name].acquire(blocking=False):
            return False
        try:
            self._check(self.sources[name])
        finally:
            self.locks[name].release()
        return True

    def _check(self, s: Source) -> None:
        st = self.state[s.name]
        if st["day"] != self.today():
            st.update(day=self.today(), today_items=0, today_skipped=0)
        error = ""
        try:
            vac = {v["id"]: v for v in store.list_vacancies(self.db) if v["is_open"]}
            got = s.fetch(self.env, vac, lambda ref: store.seen(self.db, s.name, ref))
            for ref, why in got.skipped:
                st["today_skipped"] += 1
                st["recent"].appendleft(self._entry(s.name, None, None, "skipped", why))
            groups: dict[str, list[Item]] = {}  # вложения одного письма — одна пачка
            for it in got.items:
                groups.setdefault(it.ref, []).append(it)
            for group in groups.values():
                bid, futures = self.take(group, s.source)
                st["today_items"] += len(group)
                st["recent"].appendleft(self._entry(s.name, group[0].applied, bid))
                _on_all(
                    futures,
                    lambda it=group[0], bid=bid, fs=futures: self._done(s, got, it, bid, fs),
                )
            store.mark_seen(
                self.db, s.name, [r for r, _ in got.skipped] + [i.ref for i in got.items]
            )
            if got.queued and got.items:
                got.queued([i.ref for i in got.items])
            error = self._scrub(got.error)
        except Exception as e:  # noqa: BLE001 — сбой источника не валит сервис, виден в статусе
            error = self._scrub(f"{type(e).__name__}: {e}")
            log.warning("источник %s: %s", s.name, error)
        if error and error != st["last_error"]:
            store.note(self.db, "source_error", source=s.name, error=error)
        st.update(last_check=datetime.now(UTC).isoformat(), last_error=error)

    def _done(
        self, s: Source, got: Fetched, it: Item, bid: str, futures: list[Future[Any]]
    ) -> None:
        try:
            if any(f.cancelled() for f in futures):  # сервис остановился — заберём снова
                store.forget_seen(self.db, s.name, it.ref)
            elif got.done:
                files = self.files(bid) or []
                got.done(it, all(f.state == "done" for f in files))
        except Exception as e:  # noqa: BLE001 — колбэк в потоке разбора
            log.warning("источник %s: после разбора: %s", s.name, self._scrub(str(e)))

    def _scrub(self, text: str) -> str:
        """Пароль и токены из окружения не попадают в статус, журнал и логи."""
        for k, v in self.env.items():
            if v and k.startswith("RVM_") and any(w in k for w in ("PASSWORD", "TOKEN")):
                text = text.replace(v, "***")
        return text

    @staticmethod
    def _entry(
        source: str, vacancy: str | None, bid: str | None, state: str = "", message: str = ""
    ) -> dict[str, Any]:
        at = datetime.now(UTC).isoformat()
        return {"at": at, "source": source, "vacancy": vacancy, "batch_id": bid, "state": state,
                "message": message}  # fmt: skip

    def view(self) -> list[dict[str, Any]]:
        """Состояние всех источников; имя файла и состояние элемента — из его пачки (удаление
        кандидата убирает имя файла и оттуда)."""
        out = []
        for name, s in self.sources.items():
            st = self.state[name]
            today = st["day"] == self.today()
            out.append(
                {
                    "name": name,
                    "title": s.title,
                    "account": self.env.get(s.account, "") if s.account else "",
                    "every_minutes": self.minutes,
                    "configured": not self.missing(name),
                    "needs": self.missing(name),
                    "running": self.running(name),
                    "last_check": st["last_check"],
                    "last_error": st["last_error"],
                    "today_items": st["today_items"] if today else 0,
                    "today_skipped": st["today_skipped"] if today else 0,
                    "recent": [self._live(e) for e in list(st["recent"])],
                }
            )
        return out

    def _live(self, e: dict[str, Any]) -> dict[str, Any]:
        if not e["batch_id"]:
            return {**e, "name": "", "candidate_id": None}
        files = self.files(e["batch_id"]) or []
        states = {f.state for f in files}
        state = next((x for x in ("error", "reading", "queued") if x in states), "done")
        message = "; ".join(f.message for f in files if f.state == "error")
        cid = next((f.candidate_id for f in files if f.candidate_id), None)
        return {**e, "name": ", ".join(f.name for f in files), "state": state, "message": message,
                "candidate_id": cid}  # fmt: skip
