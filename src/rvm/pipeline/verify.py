"""Проверка цитат: цитата должна стоять в указанной строке резюме."""

import re
import unicodedata

from rapidfuzz import fuzz

from rvm.facts import ResumeFacts
from rvm.models import Document, Evidence, Line, MatchRow

LETTER_BLOCK = "Сопроводительное письмо"
FUZZ_MIN = 90
FUZZ_MIN_OCR = 85

_QUOTES = str.maketrans({c: '"' for c in "«»“”„‟″'‘’‚`"})
_DASHES = re.compile("[‐-―−﹣－]")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Регистр, кавычки, дефисы и пробелы — к одному виду."""
    text = unicodedata.normalize("NFKC", text).lower().translate(_QUOTES)
    return _SPACES.sub(" ", _DASHES.sub("-", text)).strip()


def _mapped(text: str) -> tuple[str, list[int]]:
    """Как normalize, но посимвольно: нормализованный текст и индекс исходного символа."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                idx.append(i)
            continue
        for c in _DASHES.sub("-", unicodedata.normalize("NFKC", ch).lower().translate(_QUOTES)):
            out.append(" " if c.isspace() else c)
            idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def _widen(t: str, a: int, b: int) -> tuple[int, int]:
    """Расширить кусок до границ слов."""
    while a > 0 and t[a - 1].isalnum():
        a -= 1
    while b < len(t) and t[b].isalnum():
        b += 1
    return a, b


def locate(line: Line, quote: str) -> str | None:
    """Кусок настоящей строки, который цитирует модель, или None.

    Точное вхождение по границам слов (не \\b: ломает «C++», «1С»), иначе нечёткое
    в пределах строки — порог ниже для OCR. Цитата от 3 символов, длиннее строки — нет.
    """
    q, _ = _mapped(quote)
    t, idx = _mapped(line.text)
    if len(q) < 3 or not re.search(r"\w", q) or len(q) > len(t):
        return None
    m = re.search(rf"(?<!\w){re.escape(q)}(?!\w)", t)
    if m:
        a, b = m.span()
    else:
        al = fuzz.partial_ratio_alignment(q, t)
        a, b = _widen(t, al.dest_start, al.dest_end)
        # сверяем с целыми словами: «go» не пройдёт как кусок «google»
        if fuzz.ratio(q, t[a:b]) < (FUZZ_MIN_OCR if line.ocr else FUZZ_MIN):
            return None
    return line.text[idx[a] : idx[b - 1] + 1]


def quote_ok(line: Line, quote: str) -> bool:
    return locate(line, quote) is not None


def check(doc: Document, facts: ResumeFacts, line: int, quote: str) -> Evidence | None:
    """Проверенное доказательство с источником (резюме или письмо) либо None.
    В quote кладётся текст из строки резюме, а не из ответа модели."""
    found = next((ln for ln in doc.lines if ln.n == line), None)
    text = found and locate(found, quote)
    if not found or not text:
        return None
    letter = found.block == LETTER_BLOCK or line in facts.letter_lines
    return Evidence(line=line, quote=text, source="letter" if letter else "resume")


def verified_share(rows: list[MatchRow]) -> float:
    """Доля строк с проверенными цитатами среди строк, где модель цитировала."""
    failed = sum("quote_not_found" in r.flags for r in rows)
    ok = sum(bool(r.evidence) and "quote_not_found" not in r.flags for r in rows)
    return ok / (ok + failed) if ok + failed else 1.0
