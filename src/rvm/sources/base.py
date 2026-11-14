"""Общие типы источников откликов."""

from collections.abc import Callable, Mapping
from typing import NamedTuple


class Item(NamedTuple):
    """Файл из источника. ref — ключ «уже забрали» в source_seen; у вложений одного письма ref
    общий. applied — id открытой вакансии отклика или None."""

    ref: str
    name: str
    data: bytes
    applied: str | None


class Fetched(NamedTuple):
    items: list[Item]
    skipped: list[tuple[str, str]]  # (ref, причина): письмо без вложений и т. п.
    # после постановки элементов в очередь — refs поставленных (почта: флаг \Seen)
    queued: Callable[[list[str]], None] | None = None
    # после разбора всех файлов элемента: ok — без ошибок (папка: перенос в done/ или error/)
    done: Callable[[Item, bool], None] | None = None


# fetch(окружение, открытые вакансии {id: название}, seen(ref)) → новые элементы
Fetch = Callable[[Mapping[str, str], dict[str, str], Callable[[str], bool]], Fetched]


class Source(NamedTuple):
    name: str  # ключ в API и source_seen
    title: str
    source: str  # пишется в candidates.source, как «файл» у ручной загрузки
    needs: tuple[str, ...]  # обязательные переменные окружения
    fetch: Fetch
