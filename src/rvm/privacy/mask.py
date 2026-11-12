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
# сайт без схемы на любой зоне: личный (ivanov.dev) от сайта работодателя не отличить —
# маскируем любой, кроме навыков справочника (_skill_token)
# начало только с начала токена: иначе на длинном «a-a-a…» квадратичный откат
_SITE = re.compile(r"(?i)(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:[a-z]{2,12}|рф)\b(?:/\S*)?")
# ник без «@» после метки мессенджера или сети: «Telegram: ivan_petrov»
_NICK_LABEL = re.compile(
    r"(?i)(?P<label>(?<![\w-])(?:telegram|телеграмм?|tg|тг|skype|скайп|github|gitlab|vk|вк"
    r"|vkontakte|вконтакте|whatsapp|instagram|linkedin|discord|twitter)\s*:\s*)"
    r"(?P<nick>(?=[\w.:-]*[a-z_])[a-z0-9_.:-]*[a-z0-9_])"
)
# научные номера: DOI и ORCID целиком (остальные длинные цифры закрывает общий «[номер]»)
_DOI = re.compile(r"(?i)\b(?:doi:?\s*)?10\.\d{4,9}/[^\s,;]+")
_ORCID = re.compile(r"(?i)\b\d{4}-\d{4}-\d{4}-\d{3}[\dx]\b")
# почтовые домены, которые пишут без зоны: «ivan@mail»
_MAILBOX = r"(?:mail|gmail|yandex|ya|bk|inbox|list|rambler|outlook|hotmail|icloud|yahoo|proton)"
# почта и с пробелами вокруг «@»: «ivan @ mail.ru»
_EMAIL = re.compile(rf"(?<![\w.+-])[\w.+-]+\s*@\s*(?:[\w-]+(?:\.[\w-]+)+|{_MAILBOX}\b)")
_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+\d{1,3}|[78])[\s-]*\(?\d{3}\)?|\(\d{3}\))(?:[\s-]*\d){7}(?!\d)"
)
# любой «@токен» — ник, кроме статического белого списка (с учётом регистра): аннотации
# Java/Angular, CSS-правила, npm-область @angular. Справочник навыков не используем: одна
# опечатка в нём или подстановка открыла бы ник наружу, и маска с проверкой совпадают
# «@ ivan» (через пробел) — тоже ник; «@ 2020» — нет, это одиночный «@» перед числом
_NICK = re.compile(r"(?<![\w.+-])@(?:[\w.-]*\w|\s+(?!\d)[\w.-]*\w)")
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
        "angular",
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
    ("номер", _DOI),
    ("номер", _ORCID),
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
_AT_LEAK = re.compile(r"@(?!\s*(?:\d|$))")
# любой «слово.зона», в т. ч. после «.»/«-» внутри цепочки; начало слова — против отката
_ROUGH_DOMAIN = re.compile(r"(?i)(?<!\w)\w+\.(?:[a-z]{2,12}|рф)(?!\w)")
# латинские слова имени файла — не ФИО (остальная латиница имени файла закрывается)
_NOT_NAMES = frozenset(
    re.findall(
        r"\w+",
        "cv resume rezume summary portfolio developer engineer programmer frontend backend"
        " fullstack full stack senior middle junior lead teamlead techlead analyst manager"
        " designer qa tester devops intern head chief specialist data scientist software web"
        " mobile final new updated update version ver copy draft hh ru en eng rus my the and of"
        " for in",
    )
)
_MIN_KEY = 16
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)?")
_NAME_TAGS = ("Name", "Surn", "Patr")
_PATR_END = re.compile(r"(?:ович|евич|ьич|овна|евна|ична)$")
_HEAD = 5  # шапка: первые непустые строки, где ищем латинское «Имя Фамилия»
_LATIN = re.compile(r"[A-Za-z]+")
_TR = dict(
    re.findall(
        r"(\w+):(\w+)",
        "shch:щ sch:щ zh:ж kh:х ts:ц ch:ч sh:ш yu:ю ya:я yo:ё iy:ий a:а b:б v:в g:г d:д e:е z:з"
        " i:и k:к l:л m:м n:н o:о p:п r:р s:с t:т u:у f:ф h:х x:кс c:к w:в q:к y:ы j:й",
    )
)
_TR_RE = re.compile("|".join(sorted(_TR, key=len, reverse=True)))


class PrivacyError(Exception):
    """После маски остались персональные данные: текст во внешнюю модель не отправляем."""


class Masked(BaseModel):
    doc: Document
    # таблица замен (строка, тип, исходное) — только локально, наружу не отдаём
    replacements: list[tuple[int, str, str]]
    contact_hashes: list[str]
    found: dict[str, int]
    # ФИО из имени файла — только локально, для проверки assert_clean
    name_words: list[str] = []


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


def _translit(word: str) -> str:
    """Латиница → кириллица, грубо: Dmitry → дмитрий, Kuznetsova → кузнецова."""
    w = re.sub(r"ia$", "iya", word.lower())
    w = re.sub(r"(?<=[^aeiouy])y$", "iy", w)
    w = re.sub(r"(?<=[aeiou])y(?![aeiou])", "j", w)
    return _TR_RE.sub(lambda m: _TR[m.group()], w)


def _name_tags(word: str) -> set[str]:
    """Теги имени/фамилии/отчества. Незнакомому слову верим только как фамилии или отчеству
    с типичным окончанием (догадка словаря «питхон — имя» не годится); латиницу — по транслиту
    и только если словарь знает слово."""
    if _LATIN.fullmatch(word):
        word = _translit(word)
        if not _morph().word_is_known(word):
            return set()
    tags = {t for p in _morph().parse(word) for t in _NAME_TAGS if t in p.tag}
    if not _morph().word_is_known(word):
        tags &= {"Surn", "Patr"} if _PATR_END.search(word.lower()) else {"Surn"}
    return tags


def _pair_spans(text: str, head: bool) -> list[tuple[int, int]]:
    """ФИО без NER в любом регистре: отчество; пара «имя + фамилия/отчество» с заглавной
    (и ЗАГЛАВНЫМИ) или строчными рядом с отчеством («петров иван сергеевич»); латиница —
    только в шапке."""
    words = list(_WORD.finditer(text))
    tags, upper = [], []
    for m in words:
        w = m.group()
        # одна буква — только инициал с точкой («Иванов И.И.»), иначе «В 2020 г.» — отчество
        ok = (head or not _LATIN.fullmatch(w)) and (
            len(w) > 1 or text[m.end() : m.end() + 1] == "."
        )
        tags.append(_name_tags(w) if ok else set())
        upper.append(w[0].isupper())
    out = {i for i, t in enumerate(tags) if "Patr" in t and len(words[i].group()) > 1}
    # два прохода: строчная тройка ФИО цепляется от отчества в обе стороны
    for order in (range(1, len(words)), range(len(words) - 1, 0, -1)):
        for i in order:
            a, b = tags[i - 1], tags[i]
            between = text[words[i - 1].end() : words[i].start()]
            pair = "Name" in a and b - {"Name"} or "Name" in b and a - {"Name"}
            anchored = upper[i - 1] and upper[i] or bool({i - 1, i} & out)
            if pair and anchored and re.fullmatch(r"\.?\s*", between):
                out |= {i - 1, i}
    return [(words[i].start(), words[i].end()) for i in sorted(out)]


def _filename_words(filename: str) -> list[str]:
    """ФИО из имени файла: кириллица с заглавной, которую словарь знает как имя или не знает
    вовсе, и любая латиница, кроме _NOT_NAMES. Навыки справочника не берём: «Python»."""
    stem = Path(filename).stem
    words = re.findall(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?", stem)
    # латиница: целое слово и его части по смене регистра («IvanovPetr», «McDonald», «Li»)
    for tok in re.findall(r"[A-Za-z]{2,}", stem):
        parts = re.findall(r"[A-Z]{2,}(?![a-z])|[A-Z]?[a-z]+", tok)
        words += [tok, *(w for w in parts if len(w) >= 2 and w != tok)]
    out = []
    for w in words:
        if _LATIN.fullmatch(w):
            name = w.lower() not in _NOT_NAMES
        else:
            name = _is_name(w) or not _morph().word_is_known(w)
        if name and canonical(w) is None:
            out.append(w)
    return out


def _raw_words(words: list[str]) -> re.Pattern[str] | None:
    """Латинские слова имени файла как есть, без учёта регистра, по границам букв."""
    latin = [re.escape(w) for w in words if _LATIN.fullmatch(w)]
    if not latin:
        return None
    return re.compile(r"(?i)(?<![^\W\d_])(?:" + "|".join(latin) + r")(?![^\W\d_])")


def _person_spans(text: str) -> list[tuple[int, int]]:
    """Персоны по Natasha; берём только те, где словарь узнаёт имя, фамилию или отчество."""
    from natasha import Doc

    segmenter, tagger = _ner()
    doc = Doc(text)
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    spans = [s for s in doc.spans if s.type == "PER"]
    return [(s.start, s.stop) for s in spans if any(_is_name(w) for w in _WORD.findall(s.text))]


def _name_tokens(
    text: str, known: list[set[str]], head: bool = False
) -> list[tuple[int, int, int | None]]:
    """Слова-имена: (начало, конец, индекс слова ФИО из файла или None для чужих персон)."""
    persons = _person_spans(text) + _pair_spans(text, head)
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


def _skill_token(text: str, m: re.Match[str]) -> bool:
    """Доменоподобный токен — навык справочника целиком: Socket.io, ASP.NET, «Mail.ru Cloud».
    Файлы (main.py) и члены классов (React.memo) не исключаем: от домена их не отличить."""
    token = m.group().split("/")[0]
    tail = re.match(r"\s+\w+", text[m.end() :])
    return canonical(token) is not None or bool(tail and canonical(token + tail.group()))


def _at_allowed(token: str) -> bool:
    """«@токен» из статического белого списка — не ник."""
    return token[1:] in _AT_ALLOWED


def _long_number(run: str) -> bool:
    """Цепочка из 7+ цифр, не дата и не сумма: телефон, грант, ISBN, ИНН."""
    return sum(c.isdigit() for c in run) >= 7 and not _is_date_or_sum(run)


def _mask_line(
    text: str, known: list[set[str]], head: bool = False, raw: re.Pattern[str] | None = None
) -> tuple[str, list[tuple[str, str]]]:
    hits: list[tuple[str, str]] = []

    def sub(kind: str, pattern: re.Pattern[str], s: str) -> str:
        def rep(m: re.Match[str]) -> str:
            if pattern in (_SITE, _ROUGH_DOMAIN) and _skill_token(s, m):
                return m.group()
            if pattern is _NICK and _at_allowed(m.group()):
                return m.group()
            if pattern is _DIGIT_RUN and not _long_number(m.group()):
                return m.group()
            hits.append((kind, m.group()))
            return f"[{kind}]"

        return pattern.sub(rep, s)

    near_age = bool(_SEX.search(text) or _BIRTH.search(text)) or "[пол]" in text
    near_age = near_age or "[дата рождения]" in text
    for kind, pattern in _PATTERNS:
        text = sub(kind, pattern, text)
    text = sub("ник", _NICK, text)

    def label_nick(m: re.Match[str]) -> str:
        hits.append(("ник", m.group("nick")))
        return m.group("label") + "[ник]"

    text = _NICK_LABEL.sub(label_nick, text)
    if near_age:
        text = sub("возраст", _AGE_NEAR, text)
    # всё, что ловит грубая проверка цифр, закрываем: иначе ложная ошибка обезличивания
    text = sub("номер", _DIGIT_RUN, text)
    text = sub("ссылка", _ROUGH_DOMAIN, text)
    if raw is not None:
        text = sub("ФИО", raw, text)
    for start, end, _ in reversed(_name_tokens(text, known, head)):
        hits.append(("ФИО", text[start:end]))
        text = f"{text[:start]}[ФИО]{text[end:]}"
    return text, hits


def contact_hash(value: str) -> str:
    """HMAC-SHA256 нормализованного телефона или почты с ключом RVM_HASH_SALT (склейка дублей)."""
    value = value.strip().lower()
    if "@" in value:
        value = re.sub(r"\s+", "", value)
    else:
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


def _heads(lines: list[Line]) -> list[bool]:
    """Строка в шапке: среди первых _HEAD непустых."""
    out, seen = [], 0
    for line in lines:
        out.append(seen < _HEAD)
        seen += bool(line.text.strip())
    return out


def mask(doc: Document, filename: str | None = None, *, names_from_file: bool = True) -> Masked:
    """Новый Document с теми же строками, где персональные данные заменены масками.
    Вакансии маскируем без слов из имени (names_from_file=False): там название, а не ФИО."""
    words = _filename_words(filename or doc.name) if names_from_file else []
    known = [_lemmas(w) for w in words]
    raw = _raw_words(words)
    lines, table = [], []
    for line, head in zip(doc.lines, _heads(doc.lines), strict=True):
        text, hits = _mask_line(line.text, known, head, raw)
        lines.append(line.model_copy(update={"text": text}))
        table += [(line.n, kind, value) for kind, value in hits]
    warnings = [_mask_line(w, known, raw=raw)[0] for w in doc.warnings]
    hashes = sorted({contact_hash(v) for _, k, v in table if k in ("телефон", "почта")})
    update = {"lines": lines, "warnings": warnings, "name": _neutral_name(lines)}
    return Masked(
        doc=doc.model_copy(update=update),
        replacements=table,
        contact_hashes=hashes,
        found=dict(Counter(kind for _, kind, _ in table)),
        name_words=words,
    )


def _is_date_or_sum(run: str) -> bool:
    """Годы и даты («01.2020 - 12.2022») или сумма с тысячами («1 500 000») — не телефон."""
    groups = re.findall(r"\d+", run)
    years = [g for g in groups if len(g) == 4 and g[:2] in ("19", "20")]
    if years and all(len(g) <= 2 or g in years for g in groups):
        return True
    return len(groups[0]) <= 3 and groups[-1] == "000" and all(len(g) == 3 for g in groups[1:])


def _rough_hits(text: str, head: bool = False) -> list[str]:
    """Проверки, которые не зависят от детекторов маски: ловят то, что маска пропустила."""
    hits = ["цифры" for m in _DIGIT_RUN.finditer(text) if _long_number(m.group())]
    hits += ["почта"] * len(_ROUGH_EMAIL.findall(text))
    # любой «@», кроме белого списка и масок, — утечка (без опоры на детектор ника);
    # одиночный «@» перед числом или в конце строки («Сбер @ 2020») — не утечка
    rest = _NICK.sub(lambda m: "" if _at_allowed(m.group()) else m.group(), _MASK.sub("", text))
    hits += ["@"] * len(_AT_LEAK.findall(rest))
    hits += ["домен" for m in _ROUGH_DOMAIN.finditer(text) if not _skill_token(text, m)]
    hits += ["ник"] * len(_NICK_LABEL.findall(text))
    # ФИО без NER: отчество, пара «имя + фамилия» в любом регистре, латиница в шапке
    hits += ["ФИО"] * len(_pair_spans(text, head))
    return hits


def assert_clean(masked: Masked) -> None:
    """Повторный прогон детекторов и грубые проверки по тексту, имени и предупреждениям;
    нашлось — PrivacyError."""
    known = [_lemmas(v) for _, kind, v in masked.replacements if kind == "ФИО"]
    known += [_lemmas(w) for w in _filename_words(masked.doc.name)]
    raw = _raw_words(masked.name_words)
    left: Counter[str] = Counter()
    lines = masked.doc.lines
    texts = [(masked.doc.name, False), *((w, False) for w in masked.doc.warnings)]
    texts += [(ln.text, head) for ln, head in zip(lines, _heads(lines), strict=True)]
    for text, head in texts:
        _, hits = _mask_line(text, known, head)
        left.update(kind for kind, _ in hits)
        left.update(_rough_hits(text, head))
        # сырые латинские слова имени файла — без опоры на маску и транслит
        left.update("ФИО" for _ in (raw.finditer(text) if raw else ()))
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
