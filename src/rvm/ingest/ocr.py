"""Распознавание сканов и фото: Tesseract rus+eng, уверенность по словам."""

import io
import math
import time
from collections.abc import Iterable, Iterator

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps

from rvm.ingest.limits import MAX_OCR_PAGES, MAX_PIXELS, OCR_PAGE_SECONDS, OCR_TOTAL_SECONDS
from rvm.ingest.normalize import to_lines
from rvm.models import Document, FileStatus

LANGS = "rus+eng"
DPI = 300
MIN_CONFIDENCE = 60.0
NO_TEXT_LAYER = "no_text_layer"
LOW_QUALITY = "скан плохого качества, проверьте вручную"
SLOW = "скан распознаётся слишком долго"


def ocr_ready() -> str | None:
    """None, если Tesseract и языки на месте; иначе — понятная причина для рекрутера."""
    try:
        langs = set(pytesseract.get_languages())
    except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError, OSError):
        return "распознавание сканов недоступно: не установлен Tesseract"
    missing = [lang for lang in LANGS.split("+") if lang not in langs]
    if missing:
        return (
            f"распознавание сканов недоступно: у Tesseract нет языка {', '.join(missing)} "
            "(положите traineddata и задайте TESSDATA_PREFIX)"
        )
    return None


def _refuse(name: str, status: FileStatus, message: str) -> Document:
    return Document(name=name, lines=[], status=status, warnings=[message])


def read_image(name: str, data: bytes) -> Document:
    try:
        img = Image.open(io.BytesIO(data))  # читает только заголовок
    except Image.DecompressionBombError:
        return _refuse(name, FileStatus.BROKEN, "картинка на сотни мегапикселей, похоже на бомбу")
    if img.width * img.height > MAX_PIXELS:
        return _refuse(
            name, FileStatus.TOO_LARGE, f"картинка больше {MAX_PIXELS // 10**6} мегапикселей"
        )
    img.load()  # битая картинка падает здесь, до проверки Tesseract
    return _recognize(name, [ImageOps.exif_transpose(img)])


def read_pdf(name: str, data: bytes) -> Document:
    pdf = pdfium.PdfDocument(data)
    try:
        if len(pdf) > MAX_OCR_PAGES:
            return _refuse(
                name, FileStatus.TOO_LARGE, f"скан больше {MAX_OCR_PAGES} страниц, не распознаём"
            )
        return _recognize(name, _render(pdf))
    finally:
        pdf.close()


def _render(pdf: pdfium.PdfDocument) -> Iterator[Image.Image]:
    """Страницы по одной: 300 dpi на всю книгу в память не кладём; огромную страницу уменьшаем."""
    for i in range(len(pdf)):
        page = pdf[i]
        try:
            w, h = page.get_size()  # в пунктах
            scale = min(DPI / 72, math.sqrt(MAX_PIXELS / max(w * h, 1)) * 0.99)
            img = page.render(scale=scale).to_pil()
        finally:
            page.close()
        yield img


def _recognize(name: str, pages: Iterable[Image.Image]) -> Document:
    reason = ocr_ready()
    if reason is not None:
        return Document(
            name=name, lines=[], status=FileStatus.EMPTY, warnings=[NO_TEXT_LAYER, reason]
        )
    texts: list[str] = []
    confs: list[float] = []
    deadline = time.monotonic() + OCR_TOTAL_SECONDS
    for page in pages:
        timeout = min(OCR_PAGE_SECONDS, deadline - time.monotonic())
        try:
            if timeout <= 0:
                return _refuse(name, FileStatus.TOO_LARGE, SLOW)
            page_lines, page_confs = _page_lines(page, timeout)
        except RuntimeError as e:  # pytesseract сообщает о timeout только текстом RuntimeError
            if "timeout" not in str(e):
                raise
            return _refuse(name, FileStatus.TOO_LARGE, SLOW)
        texts += page_lines
        confs += page_confs
    lines = to_lines(texts)
    if not lines:
        return Document(name=name, lines=[], status=FileStatus.EMPTY, warnings=[NO_TEXT_LAYER])
    for ln in lines:
        ln.ocr = True
    conf = sum(confs) / len(confs)
    warnings = [f"текст распознан со скана, уверенность {conf:.0f}%"]
    status = FileStatus.OK
    if conf < MIN_CONFIDENCE:
        status = FileStatus.SCAN_LOW_QUALITY
        warnings.append(LOW_QUALITY)
    return Document(name=name, lines=lines, status=status, warnings=warnings)


def _page_lines(img: Image.Image, timeout: float) -> tuple[list[str], list[float]]:
    """Слова Tesseract → строки страницы по порядку и уверенности слов."""
    data = pytesseract.image_to_data(
        img, lang=LANGS, output_type=pytesseract.Output.DICT, timeout=timeout
    )
    rows: dict[tuple[int, int, int], list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        conf = float(data["conf"][i])
        if not word.strip() or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        rows.setdefault(key, []).append(word)
        confs.append(conf)
    return [" ".join(words) for words in rows.values()], confs
