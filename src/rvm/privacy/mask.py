"""Маскирование ФИО, контактов, возраста, пола и адреса внутри строк (номера строк не меняются).

Любой «@токен» вне белого списка — ник. Город не маскируем: он нужен для условий вакансии.
Родовые окончания глаголов («работала») не трогаем — это известное ограничение.
"""

import hashlib
import hmac
import os
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rvm.models import Document, Line
from rvm.refs.skills import canonical

_LINK = re.compile(
    r"(?i)(?:https?://|www\.)\S+"
    r"|\b(?:t\.me|telegram\.me|vk\.com|vk\.ru|github\.com|gitlab\.com|linkedin\.com"
    r"|hh\.ru/resume|habr\.com)(?:/\S*)?"
)
# сайт без схемы: личный (ivanov.ru) от сайта работодателя не отличить — маскируем любой
# начало только с начала токена: иначе на длинном «a-a-a…» квадратичный откат
_SITE = re.compile(r"(?i)(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:ru|com|рф|me|io|org|net|su)\b(?:/\S*)?")
# почтовые домены, которые пишут без зоны: «ivan@mail»
_MAILBOX = r"(?:mail|gmail|yandex|ya|bk|inbox|list|rambler|outlook|hotmail|icloud|yahoo|proton)"
_EMAIL = re.compile(rf"(?<![\w.+-])[\w.+-]+@(?:[\w-]+(?:\.[\w-]+)+|{_MAILBOX}\b)")
_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+\d{1,3}|[78])[\s-]*\(?\d{3}\)?|\(\d{3}\))(?:[\s-]*\d){7}(?!\d)"
)
# любой «@токен» — ник, кроме белого списка: аннотации Java/Angular и CSS-правила
_NICK = re.compile(r"(?<![\w.+-])@[\w.-]*\w")
_AT_ALLOWED = frozenset(
    [
        "Override",
        "Component",
        "Service",
        "Repository",
        "RestController",
        "Controller",
        "Autowired",
        "Bean",
        "Entity",
        "Table",
        "Id",
        "Test",
        "Data",
        "Getter",
        "Setter",
        "Inject",
        "Input",
        "Output",
        "NgModule",
        "Injectable",
        "Transactional",
        "Configuration",
        "media",
        "import",
        "keyframes",
        "font-face",
        "supports",
    ]
)
# закрытые маски вырезаем перед поиском оставшихся «@»
_MASK = re.compile(r"\[[а-яё ]+\]")
_BIRTH = re.compile(
    r"(?i)(?:дата\s+рождения|родил(?:ся|ась))\s*:?\s*(?:в\s+)?"
    r"(?:\d{1,2}[\s./-]*(?:\d{1,2}|[а-яё]+)[\s./-]*)?\d{2,4}(?:\s*(?:года|году|г\.|г\b))?"
    r"|\b(?:\d{1,2}\.\d{1,2}\.)?\d{4}\s*г\.?\s?р\.?(?![\wё])"
)
_AGE = re.compile(r"(?i)\bвозраст\s*:?\s*\d{1,3}(?:\s*(?:год|года|лет)\b)?")
_SEX = re.compile(r"(?i)\b(?:мужчина|женщина)\b|\bпол\s*:\s*[а-яё]+")
_ADDRESS = re.compile(
    r"(?i)\b(?:ул\.|улица|пр-т|проспект|переулок|пер\.|бульвар|б-р|шоссе|наб\.|набережная"
    r"|мкр\.?|микрорайон)\s*[\wё-]+(?:\s+[А-ЯЁ][\wё-]*)*"
    r"(?:,?\s*(?:д\.|дом|корп\.|к\.|стр\.|кв\.|квартира)\s*\d+[\w/-]*)*"
    r"|(?<!\w)(?:д\.|дом|корп\.|кв\.|квартира)\s*\d+[\w/-]*"
)
# «25 лет» маскируем только рядом с полом или датой рождения: стаж «7 лет» оставляем
_AGE_NEAR = re.compile(r"(?i)\b\d{1,3}\s*(?:год|года|лет)\b")

# порядок важен: ссылки и почта раньше ников и телефонов
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ссылка", _LINK),
    ("почта", _EMAIL),
    ("ссылка", _SITE),
    ("телефон", _PHONE),
    ("дата рождения", _BIRTH),
    ("возраст", _AGE),
    ("пол", _SEX),
    ("адрес", _ADDRESS),
]
# Грубые проверки assert_clean, независимые от маски: цепочка цифр с разделителями, почта, домены
_DIGIT_RUN = re.compile(r"\d(?:[ ().-]{0,2}\d)*")
_ROUGH_EMAIL = re.compile(rf"(?<![\w.+-])[\w.+-]+@(?:\w[\w-]*\.\w{{2,}}|{_MAILBOX}\b)")
_DOMAIN = re.compile(r"(?i)(?<!\w)\w+\.(?:ru|com|рф|me|io|org|net|su)\b")
_MIN_KEY = 16
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)?")
_NAME_TAGS = ("Name", "Surn", "Patr")


class PrivacyError(Exception):
    """После маски остались персональные данные: текст во внешнюю модель не отправляем."""


class Masked(BaseModel):
    doc: Document
    # таблица замен (строка, тип, исходное) — только локально, наружу не отдаём
    replacements: list[tuple[int, str, str]]
    contact_hashes: list[str]
    found: dict[str, int]


@cache
def _morph() -> Any:
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


@cache
def _ner() -> tuple[Any, Any]:
    from natasha import NewsEmbedding, NewsNERTagger, Segmenter

    return Segmenter(), NewsNERTagger(NewsEmbedding())


def _is_name(word: str) -> bool:
    return any(tag in p.tag for p in _morph().parse(word) for tag in _NAME_TAGS)


def _lemmas(word: str) -> set[str]:
    """Само слово и леммы его разборов как имени/фамилии/отчества (для падежей)."""
    forms = {word.lower()}
    forms |= {p.normal_form for p in _morph().parse(word) if any(t in p.tag for t in _NAME_TAGS)}
    return {f.replace("ё", "е") for f in forms}


def _filename_words(filename: str) -> list[str]:
    """ФИО из имени файла: слова с заглавной, которые словарь знает как имя или не знает вовсе."""
    words = re.findall(r"[А-ЯЁA-Z][а-яёa-z]+(?:-[А-ЯЁA-Z][а-яёa-z]+)?", Path(filename).stem)
    return [w for w in words if _is_name(w) or not _morph().word_is_known(w)]


def _person_spans(text: str) -> list[tuple[int, int]]:
    """Персоны по Natasha; берём только те, где словарь узнаёт имя, фамилию или отчество."""
    from natasha import Doc

    segmenter, tagger = _ner()
    doc = Doc(text)
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    spans = [s for s in doc.spans if s.type == "PER"]
    return [(s.start, s.stop) for s in spans if any(_is_name(w) for w in _WORD.findall(s.text))]


def _name_tokens(text: str, known: list[set[str]]) -> list[tuple[int, int, int | None]]:
    """Слова-имена: (начало, конец, индекс слова ФИО из файла или None для чужих персон)."""
    persons = _person_spans(text)
    out = []
    for m in _WORD.finditer(text):
        idx = None
        if m.group()[0].isupper():
            lem = _lemmas(m.group())
            idx = next((i for i, k in enumerate(known) if lem & k), None)
        in_person = any(a <= m.start() and m.end() <= b for a, b in persons)
        if idx is not None or in_person:
            out.append((m.start(), m.end(), idx))
    return out


def _is_skill(text: str, m: re.Match[str]) -> bool:
    """Доменоподобный токен — навык из справочника (Socket.io, ASP.NET, Mail.ru Cloud)."""
    token = m.group().split("/")[0]
    tail = re.match(r"\s+\w+", text[m.end() :])
    return canonical(token) is not None or bool(tail and canonical(token + tail.group()))


def _at_allowed(token: str) -> bool:
    """«@токен» из белого списка или навык из справочника (@python) — не ник."""
    name = token[1:]
    return name in _AT_ALLOWED or canonical(name) is not None


def _mask_line(text: str, known: list[set[str]]) -> tuple[str, list[tuple[str, str]]]:
    hits: list[tuple[str, str]] = []

    def sub(kind: str, pattern: re.Pattern[str], s: str) -> str:
        def rep(m: re.Match[str]) -> str:
            if pattern is _SITE and _is_skill(s, m) or pattern is _NICK and _at_allowed(m.group()):
                return m.group()
            hits.append((kind, m.group()))
            return f"[{kind}]"

        return pattern.sub(rep, s)

    near_age = bool(_SEX.search(text) or _BIRTH.search(text)) or "[пол]" in text
    near_age = near_age or "[дата рождения]" in text
    for kind, pattern in _PATTERNS:
        text = sub(kind, pattern, text)
    text = sub("ник", _NICK, text)
    if near_age:
        text = sub("возраст", _AGE_NEAR, text)
    for start, end, _ in reversed(_name_tokens(text, known)):
        hits.append(("ФИО", text[start:end]))
        text = f"{text[:start]}[ФИО]{text[end:]}"
    return text, hits


def contact_hash(value: str) -> str:
    """HMAC-SHA256 нормализованного телефона или почты с ключом RVM_HASH_SALT (склейка дублей)."""
    value = value.strip().lower()
    if "@" not in value:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11 and digits[0] in "78":
            digits = digits[1:]
        value = "7" + digits if len(digits) == 10 else digits
    key = os.environ.get("RVM_HASH_SALT", "")
    if len(key) < _MIN_KEY:
        raise RuntimeError(
            f"не задан ключ RVM_HASH_SALT (нужно не короче {_MIN_KEY} символов): "
            "без него отпечатки контактов подбираются перебором"
        )
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _neutral_name(lines: list[Line]) -> str:
    """Имя без ФИО: «resume_» + буквы хэша маскированного текста (цифр нет — грубая проверка
    не спутает их с телефоном; от ФИО и имени файла не зависит)."""
    digest = hashlib.sha256("\n".join(ln.text for ln in lines).encode()).hexdigest()
    return "resume_" + "".join(chr(ord("a") + int(c, 16)) for c in digest[:10])


def mask(doc: Document, filename: str | None = None) -> Masked:
    """Новый Document с теми же строками, где персональные данные заменены масками."""
    known = [_lemmas(w) for w in _filename_words(filename or doc.name)]
    lines, table = [], []
    for line in doc.lines:
        text, hits = _mask_line(line.text, known)
        lines.append(line.model_copy(update={"text": text}))
        table += [(line.n, kind, value) for kind, value in hits]
    warnings = [_mask_line(w, known)[0] for w in doc.warnings]
    hashes = sorted({contact_hash(v) for _, k, v in table if k in ("телефон", "почта")})
    update = {"lines": lines, "warnings": warnings, "name": _neutral_name(lines)}
    return Masked(
        doc=doc.model_copy(update=update),
        replacements=table,
        contact_hashes=hashes,
        found=dict(Counter(kind for _, kind, _ in table)),
    )


def _is_date_or_sum(run: str) -> bool:
    """Годы и даты («01.2020 - 12.2022») или сумма с тысячами («1 500 000») — не телефон."""
    groups = re.findall(r"\d+", run)
    years = [g for g in groups if len(g) == 4 and g[:2] in ("19", "20")]
    if years and all(len(g) <= 2 or g in years for g in groups):
        return True
    return len(groups[0]) <= 3 and groups[-1] == "000" and all(len(g) == 3 for g in groups[1:])


def _rough_hits(text: str) -> list[str]:
    """Проверки, которые не зависят от детекторов маски: ловят то, что маска пропустила."""
    hits = []
    for m in _DIGIT_RUN.finditer(text):
        if sum(c.isdigit() for c in m.group()) >= 7 and not _is_date_or_sum(m.group()):
            hits.append("цифры")
    hits += ["почта"] * len(_ROUGH_EMAIL.findall(text))
    # любой «@», кроме белого списка и масок, — утечка (без опоры на детектор ника)
    rest = _NICK.sub(lambda m: "" if _at_allowed(m.group()) else m.group(), _MASK.sub("", text))
    hits += ["@"] * rest.count("@")
    hits += ["домен" for m in _DOMAIN.finditer(text) if not _is_skill(text, m)]
    return hits


def assert_clean(masked: Masked) -> None:
    """Повторный прогон детекторов и грубые проверки по тексту, имени и предупреждениям;
    нашлось — PrivacyError."""
    known = [_lemmas(v) for _, kind, v in masked.replacements if kind == "ФИО"]
    known += [_lemmas(w) for w in _filename_words(masked.doc.name)]
    left: Counter[str] = Counter()
    texts = [masked.doc.name, *masked.doc.warnings, *(ln.text for ln in masked.doc.lines)]
    for text in texts:
        _, hits = _mask_line(text, known)
        left.update(kind for kind, _ in hits)
        left.update(_rough_hits(text))
    if left:
        # в сообщении только типы и число, без самих значений
        raise PrivacyError(f"после маски остались данные: {dict(left)}")


def _pick(new: list[str], idx: int, name: str) -> str:
    return new[idx] if idx < len(new) else name


def swap_identity(doc: Document, name: str, sex: str) -> Document:
    """Подменить ФИО кандидата (по имени файла) и пол («м»/«ж») в исходном документе."""
    old = _filename_words(doc.name)
    new = name.split()
    known = [_lemmas(w) for w in old]
    male = sex.lower().startswith("м")

    def swap_sex(m: re.Match[str]) -> str:
        word = m.group()
        if word.lower().startswith("пол"):
            return word[: word.index(":") + 1] + (" мужской" if male else " женский")
        out = "мужчина" if male else "женщина"
        return out.capitalize() if word[0].isupper() else out

    lines: list[Line] = []
    for line in doc.lines:
        text = _SEX.sub(swap_sex, line.text)
        for start, end, idx in reversed(_name_tokens(text, known)):
            if idx is not None:
                text = text[:start] + _pick(new, idx, name) + text[end:]
        lines.append(line.model_copy(update={"text": text}))
    # один проход по границам букв («_» в имени файла — не буква), иначе «Иван» внутри «Иванова»
    pattern = r"(?<![^\W\d_])(?:" + "|".join(map(re.escape, old)) + r")(?![^\W\d_])"
    new_name = doc.name
    if old:
        new_name = re.sub(pattern, lambda m: _pick(new, old.index(m.group()), name), doc.name)
    return doc.model_copy(update={"lines": lines, "name": new_name})
