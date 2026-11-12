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


def quote_ok(line: Line, quote: str) -> bool:
    """Точное вхождение после нормализации, иначе нечёткое — порог ниже для OCR."""
    q, t = normalize(quote), normalize(line.text)
    if not q:
        return False
    if q in t:
        return True
    # partial_ratio ищет короткую строку в длинной: цитата длиннее строки так «пройдёт» целиком
    score = fuzz.partial_ratio(q, t) if len(q) <= len(t) else fuzz.ratio(q, t)
    return score >= (FUZZ_MIN_OCR if line.ocr else FUZZ_MIN)


def check(doc: Document, facts: ResumeFacts, line: int, quote: str) -> Evidence | None:
    """Проверенное доказательство с источником (резюме или письмо) либо None."""
    found = next((ln for ln in doc.lines if ln.n == line), None)
    if found is None or not quote_ok(found, quote):
        return None
    letter = found.block == LETTER_BLOCK or line in facts.letter_lines
    return Evidence(line=line, quote=quote, source="letter" if letter else "resume")


def verified_share(rows: list[MatchRow]) -> float:
    """Доля строк с проверенными цитатами среди строк, где модель цитировала."""
    failed = sum("quote_not_found" in r.flags for r in rows)
    ok = sum(bool(r.evidence) and "quote_not_found" not in r.flags for r in rows)
    return ok / (ok + failed) if ok + failed else 1.0
