"""Источник «папка» (RVM_INBOX_DIR): выгрузка с hh, правило почтового клиента, сканер.

Забираются обычные файлы верхнего уровня, кроме скрытых (`.`) и временных (`~$…`, `….part`);
папки и символические ссылки пропускаются. Файл больше лимита пачки — сразу в `error/`.
За опрос — не больше POLL_ITEMS файлов и POLL_BYTES байт, остальное — в следующий опрос. После разбора файл переносится в `done/` (всё прочитано и оценено) или `error/`
(ошибка чтения или оценки) внутри папки; при совпадении имени к нему добавляется префикс.
ref = имя + размер + время изменения: файл с тем же именем, но новым содержимым забирается снова.

Вакансия отклика: имя `<id вакансии>__<имя>.docx` (например `3__Иванов.docx`) — отклик на
открытую вакансию 3, резюме сохранится как `Иванов.docx`. Нет префикса или вакансия не открыта —
без вакансии.
"""

import logging
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rvm.ingest import limits
from rvm.sources.base import POLL_BYTES, POLL_ITEMS, Fetched, Item

log = logging.getLogger("rvm.sources")
DONE, ERROR = "done", "error"


def _temporary(name: str) -> bool:
    return name.startswith((".", "~$")) or name.endswith(".part")


def _move(path: Path, sub: str) -> None:
    dest = path.parent / sub / path.name
    if dest.exists():
        dest = dest.with_name(f"{uuid.uuid4().hex[:8]}-{path.name}")
    path.replace(dest)


def fetch(
    env: Mapping[str, str], vacancies: dict[str, dict[str, Any]], seen: Callable[[str], bool]
) -> Fetched:
    root = Path(env["RVM_INBOX_DIR"])
    for sub in (DONE, ERROR):
        (root / sub).mkdir(parents=True, exist_ok=True)
    items, skipped, paths = [], [], {}
    used, cap = 0, limits.MAX_REQUEST_BYTES
    for p in sorted(root.iterdir()):
        if _temporary(p.name):
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):  # папки, символические ссылки, устройства
            continue
        ref = f"{p.name}|{st.st_size}|{st.st_mtime_ns}"
        if seen(ref):
            continue  # ещё разбирается
        if len(items) >= POLL_ITEMS or (items and used + st.st_size > POLL_BYTES):
            break
        try:  # ссылку, подложенную после lstat, не открываем; FIFO не ждём
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        with os.fdopen(fd, "rb") as f:
            fst = os.fstat(f.fileno())
            ok = stat.S_ISREG(fst.st_mode) and fst.st_size <= cap
            data = f.read(cap + 1) if ok else b""
        if not stat.S_ISREG(fst.st_mode):
            continue
        if not ok or len(data) > cap:  # не читаем в память больше лимита
            _move(p, ERROR)
            skipped.append((ref, f"{p.name}: больше {cap // 2**20} МБ"))
            continue
        used += len(data)
        vid, sep, rest = p.name.partition("__")
        applied = vid if sep and rest and vid in vacancies else None
        items.append(Item(ref, rest if applied else p.name, data, applied))
        paths[ref] = p

    def done(item: Item, ok: bool) -> None:
        try:
            _move(paths[item.ref], DONE if ok else ERROR)
        except OSError as e:
            log.warning("папка: файл не перенесён: %s", e)

    return Fetched(items, skipped, done=done)
