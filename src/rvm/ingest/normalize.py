"""Нормализация текста в нумерованные строки и разметка блоков hh."""

import re
import unicodedata

from rvm.models import Line

FOOTER_BLOCK = "колонтитул"

_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
_WORD_DASH = re.compile(r"(?<=\w)[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d](?=\w)")
_SPACES = re.compile(r"\s+")

# Заголовки блоков hh: (каноническое имя, допускается ли любой хвост после имени).
_HEADERS: list[tuple[str, bool]] = [
    ("Опыт работы", False),
    ("Образование", False),
    ("Ключевые навыки", False),
    ("Навыки", False),
    ("Обо мне", False),
    ("Сопроводительное письмо", False),
    ("Желаемая должность", True),
    ("Повышение квалификации", True),
    ("Знание языков", False),
    ("Гражданство", True),
    ("Дополнительная информация", False),
]
# Хвост строгого заголовка: «— 7 лет 9 месяцев», «: …», «(…)» или сразу цифры.
_STRICT_TAIL = r"(?:\s*[—–\-:(\d].*)?"
_HEADER_RES = [
    (name, re.compile(re.escape(name) + (r"\b.*" if loose else _STRICT_TAIL), re.IGNORECASE))
    for name, loose in _HEADERS
]
_MAX_HEADER_LEN = 80


def normalize_line(text: str) -> str:
    """Одна строка: NFKC, дефисы внутри слова → «-», невидимые символы и лишние пробелы прочь."""
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    text = _WORD_DASH.sub("-", text)
    return _SPACES.sub(" ", text).strip()


def header_block(text: str) -> str | None:
    """Имя блока, если строка — заголовок блока hh."""
    if len(text) > _MAX_HEADER_LEN:
        return None
    return next((name for name, rx in _HEADER_RES if rx.fullmatch(text)), None)


def to_lines(body: list[str], footers: list[str] | None = None) -> list[Line]:
    """Абзацы → строки с номерами с 1; колонтитулы — в конце с блоком «колонтитул»."""
    lines: list[Line] = []
    block: str | None = None
    for raw, is_footer in [(t, False) for t in body] + [(t, True) for t in footers or []]:
        for part in raw.splitlines():
            text = normalize_line(part)
            if not text:
                continue
            if is_footer:
                cur = FOOTER_BLOCK
            else:
                block = header_block(text) or block
                cur = block
            lines.append(Line(n=len(lines) + 1, text=text, block=cur))
    return lines
