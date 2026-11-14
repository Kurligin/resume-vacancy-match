"""Источник «папка» (RVM_INBOX_DIR): выгрузка с hh, правило почтового клиента, сканер.

Забираются файлы верхнего уровня, кроме скрытых (`.`), временных (`~$…`, `….part`) и вложенных
папок. После разбора файл переносится в `done/` (всё прочитано и оценено) или `error/`
(ошибка чтения или оценки) внутри папки; при совпадении имени к нему добавляется префикс.
ref = имя + размер + время изменения: файл с тем же именем, но новым содержимым забирается снова.

Вакансия отклика: имя `<id вакансии>__<имя>.docx` (например `3__Иванов.docx`) — отклик на
открытую вакансию 3, резюме сохранится как `Иванов.docx`. Нет префикса или вакансия не открыта —
без вакансии.
"""

import logging
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from rvm.ingest import limits
from rvm.sources.base import Fetched, Item

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
    env: Mapping[str, str], vacancies: dict[str, str], seen: Callable[[str], bool]
) -> Fetched:
    root = Path(env["RVM_INBOX_DIR"])
    for sub in (DONE, ERROR):
        (root / sub).mkdir(parents=True, exist_ok=True)
    items, skipped, paths = [], [], {}
    for p in sorted(root.iterdir()):
        if not p.is_file() or _temporary(p.name):
            continue
        st = p.stat()
        ref = f"{p.name}|{st.st_size}|{st.st_mtime_ns}"
        if seen(ref):
            continue  # ещё разбирается
        if st.st_size > limits.MAX_REQUEST_BYTES:  # не читаем в память
            _move(p, ERROR)
            skipped.append((ref, f"{p.name}: больше {limits.MAX_REQUEST_BYTES // 2**20} МБ"))
            continue
        vid, sep, rest = p.name.partition("__")
        applied = vid if sep and rest and vid in vacancies else None
        items.append(Item(ref, rest if applied else p.name, p.read_bytes(), applied))
        paths[ref] = p

    def done(item: Item, ok: bool) -> None:
        try:
            _move(paths[item.ref], DONE if ok else ERROR)
        except OSError as e:
            log.warning("папка: файл не перенесён: %s", e)

    return Fetched(items, skipped, done=done)
