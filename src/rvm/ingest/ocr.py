"""Распознавание сканов и фото: Tesseract rus+eng, уверенность по словам."""

import io
import math
import time
from collections.abc import Iterable, Iterator

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from rvm.ingest.limits import MAX_OCR_PAGES, MAX_PIXELS, OCR_PAGE_SECONDS, OCR_TOTAL_SECONDS
from rvm.ingest.normalize import to_lines
from rvm.models import Document, FileStatus

LANGS = "rus+eng"
DPI = 300
MIN_CONFIDENCE = 60.0
NO_TEXT_LAYER = "no_text_layer"
LOW_QUALITY = "скан плохого качества, проверьте вручную"
SLOW = "скан распознаётся слишком долго"

register_heif_opener()  # фото с iPhone (HEIC) открываются как обычные картинки


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
    reason = ocr_ready()
    if reason is not None:
        return Document(
            name=name, lines=[], status=FileStatus.EMPTY, warnings=[NO_TEXT_LAYER, reason]
        )
    return _recognize(name, [_on_white(ImageOps.exif_transpose(img))])


def _on_white(img: Image.Image) -> Image.Image:
    """Прозрачный фон (WEBP, PNG, GIF) → белый: иначе чёрный текст пропадёт на чёрном."""
    if img.mode in ("1", "L", "RGB"):
        return img
    rgba = img.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, rgba).convert("RGB")


def read_pdf(name: str, data: bytes, texts: list[str] | None = None) -> Document:
    """Скан PDF. texts — текст страниц из текстового слоя; пустые страницы распознаём."""
    pdf = pdfium.PdfDocument(data)
    try:
        texts = texts or [""] * len(pdf)
        todo = [i for i, t in enumerate(texts) if not t]
        if len(todo) > MAX_OCR_PAGES:
            return _refuse(
                name, FileStatus.TOO_LARGE, f"скан больше {MAX_OCR_PAGES} страниц, не распознаём"
            )
        reason = ocr_ready()
        if reason is not None:
            lines = to_lines(texts)
            status = FileStatus.OK if lines else FileStatus.EMPTY
            return Document(name=name, lines=lines, status=status, warnings=[NO_TEXT_LAYER, reason])
        scans = _render(pdf, todo)
        return _recognize(name, (t or next(scans) for t in texts))
    finally:
        pdf.close()


def _render(pdf: pdfium.PdfDocument, indices: list[int] | None = None) -> Iterator[Image.Image]:
    """Страницы по одной: 300 dpi на всю книгу в память не кладём; огромную страницу уменьшаем."""
    for i in range(len(pdf)) if indices is None else indices:
        page = pdf[i]
        try:
            w, h = page.get_size()  # в пунктах
            scale = min(DPI / 72, math.sqrt(MAX_PIXELS / max(w * h, 1)) * 0.99)
            img = page.render(scale=scale).to_pil()
        finally:
            page.close()
        yield img


def _recognize(name: str, pages: Iterable[Image.Image | str]) -> Document:
    """Картинки распознаём, строки (текстовые страницы PDF) берём как есть, порядок сохраняем."""
    texts: list[str] = []
    from_scan: list[bool] = []
    confs: list[float] = []
    deadline = time.monotonic() + OCR_TOTAL_SECONDS
    for page in pages:
        if isinstance(page, str):
            texts.append(page)
            from_scan.append(False)
            continue
        timeout = min(OCR_PAGE_SECONDS, deadline - time.monotonic())
        try:
            if timeout <= 0:
                return _refuse(name, FileStatus.TOO_LARGE, SLOW)
            page_lines, page_confs = _page_lines(page, timeout)
        except RuntimeError as e:  # pytesseract сообщает о timeout только текстом RuntimeError
            if "timeout" not in str(e):
                raise
            return _refuse(name, FileStatus.TOO_LARGE, SLOW)
        texts.append("\n".join(page_lines))
        from_scan.append(True)
        confs += page_confs
    lines = to_lines(texts)
    if not lines:
        return Document(name=name, lines=[], status=FileStatus.EMPTY, warnings=[NO_TEXT_LAYER])
    # Строки страницы считаем той же нормализацией: так флаг ocr ложится на свои строки.
    flags = [scan for text, scan in zip(texts, from_scan) for _ in to_lines([text])]
    for ln, scan in zip(lines, flags, strict=True):
        ln.ocr = scan
    if not confs:
        return Document(name=name, lines=lines)
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
