"""Распознавание сканов и фото: Tesseract rus+eng, уверенность по словам."""

import io
from collections.abc import Iterable, Iterator

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps

from rvm.ingest.normalize import to_lines
from rvm.models import Document, FileStatus

LANGS = "rus+eng"
DPI = 300
MIN_CONFIDENCE = 60.0
NO_TEXT_LAYER = "no_text_layer"
LOW_QUALITY = "скан плохого качества, проверьте вручную"


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


def read_image(name: str, data: bytes) -> Document:
    img = Image.open(io.BytesIO(data))
    img.load()  # битая картинка падает здесь, до проверки Tesseract
    return _recognize(name, [ImageOps.exif_transpose(img)])


def read_pdf(name: str, data: bytes) -> Document:
    pdf = pdfium.PdfDocument(data)
    return _recognize(name, _render(pdf))


def _render(pdf: pdfium.PdfDocument) -> Iterator[Image.Image]:
    """Страницы по одной: 300 dpi на всю книгу в память не кладём."""
    for i in range(len(pdf)):
        yield pdf[i].render(scale=DPI / 72).to_pil()


def _recognize(name: str, pages: Iterable[Image.Image]) -> Document:
    reason = ocr_ready()
    if reason is not None:
        return Document(
            name=name, lines=[], status=FileStatus.EMPTY, warnings=[NO_TEXT_LAYER, reason]
        )
    texts: list[str] = []
    confs: list[float] = []
    for page in pages:
        page_lines, page_confs = _page_lines(page)
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


def _page_lines(img: Image.Image) -> tuple[list[str], list[float]]:
    """Слова Tesseract → строки страницы по порядку и уверенности слов."""
    data = pytesseract.image_to_data(img, lang=LANGS, output_type=pytesseract.Output.DICT)
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
