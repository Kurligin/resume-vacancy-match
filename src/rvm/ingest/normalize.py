"""Нормализация текста в нумерованные строки и разметка блоков резюме."""

import re
import unicodedata

from rvm.models import Line

FOOTER_BLOCK = "колонтитул"

_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
_WORD_DASH = re.compile(r"(?<=\w)[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d](?=\w)")
_SPACES = re.compile(r"\s+")

# Заголовки блоков: каноническое имя (как в hh) → варианты написания. Блок «Обо мне» —
# самоописание кандидата, на него опирается сопоставление.
_HEADERS: dict[str, list[str]] = {
    "Опыт работы": [
        "Опыт работы",
        "Опыт",
        "Трудовая деятельность",
        "Места работы",
        "Work experience",
        "Experience",
        "Employment history",
    ],
    "Образование": ["Образование", "Education"],
    "Ключевые навыки": ["Ключевые навыки", "Key skills"],
    "Навыки": [
        "Навыки",
        "Профессиональные навыки",
        "Технические навыки",
        "Hard skills",
        "Skills",
        "Technical skills",
    ],
    "Обо мне": [
        "Обо мне",
        "О себе",
        "Личные качества",
        "Soft skills",
        "About me",
        "About",
        "Summary",
        "Profile",
    ],
    "Сопроводительное письмо": ["Сопроводительное письмо", "Cover letter"],
    "Желаемая должность": ["Желаемая должность"],
    "Повышение квалификации": [
        "Повышение квалификации",
        "Курсы",
        "Сертификаты",
        "Courses",
        "Certificates",
    ],
    "Знание языков": ["Знание языков", "Языки", "Иностранные языки", "Languages"],
    "Гражданство": ["Гражданство"],
    "Дополнительная информация": [
        "Дополнительная информация",
        "Дополнительно",
        "Additional information",
    ],
}
# Хвост после названия: «:» без продолжения, стаж «— 7 лет», «: 2 года» или «(7 лет)».
# «Гражданство: Россия», «Навыки: Python» — текст, а не заголовок.
_TAIL = r"\s*:?|\s*[—–\-:]?\s*\d.*|\s*\(.*\)\s*:?"
# Заголовки hh с продолжением: «Гражданство, время в пути до работы», «… и зарплата».
_LOOSE = {"Повышение квалификации", "Гражданство"}
_LOOSE_TAIL = r"|\s*,.*|\s+и\s+.*"
# «Желаемая должность: Бухгалтер» открывает раздел анкеты hh вместе с названием должности.
_ANY_TAIL = {"Желаемая должность"}


def _tail(name: str) -> str:
    if name in _ANY_TAIL:
        return r"\b.*"
    return _TAIL + (_LOOSE_TAIL if name in _LOOSE else "")


_HEADER_RES = [
    (name, re.compile(rf"(?:{'|'.join(map(re.escape, variants))})(?:{_tail(name)})", re.IGNORECASE))
    for name, variants in _HEADERS.items()
]
_MAX_HEADER_LEN = 80


def normalize_line(text: str) -> str:
    """Одна строка: NFKC, дефисы внутри слова → «-», невидимые символы и лишние пробелы прочь."""
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    text = _WORD_DASH.sub("-", text)
    return _SPACES.sub(" ", text).strip()


def header_block(text: str) -> str | None:
    """Каноническое имя блока, если строка целиком — заголовок блока."""
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
