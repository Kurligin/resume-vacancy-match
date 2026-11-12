"""Тесты распознавания сканов: резюме кейса, отрисованное в картинку, через OCR."""

import io
import os
import textwrap
from pathlib import Path
from typing import Any

import pytesseract
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfWriter
from rapidfuzz import fuzz

from rvm.ingest import load_bytes, load_path, ocr
from rvm.ingest.ocr import ocr_ready
from rvm.models import Document, FileStatus

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_ocr = pytest.mark.skipif(ocr_ready() is not None, reason="нет tesseract или языка rus")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")

_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_W, _H, _MARGIN, _SIZE, _STEP = 2480, 3508, 150, 40, 58  # A4 при 300 dpi


def _font() -> ImageFont.FreeTypeFont:
    path = next((p for p in _FONTS if Path(p).exists()), None)
    if path is None:
        pytest.skip("нет шрифта с кириллицей")
    return ImageFont.truetype(path, _SIZE)


def _pages(text_lines: list[str]) -> list[Image.Image]:
    """Текст → страницы A4: перенос по ширине, чёрным по белому."""
    font = _font()
    wrapped = [w for t in text_lines for w in textwrap.wrap(t, 90)]
    per_page = (_H - 2 * _MARGIN) // _STEP
    pages = []
    for i in range(0, len(wrapped), per_page):
        img = Image.new("L", (_W, _H), 255)
        draw = ImageDraw.Draw(img)
        for j, line in enumerate(wrapped[i : i + per_page]):
            draw.text((_MARGIN, _MARGIN + j * _STEP), line, font=font, fill=0)
        pages.append(img)
    return pages


def _noisy(img: Image.Image) -> Image.Image:
    noise = Image.effect_noise(img.size, 60)
    img = Image.blend(img, noise, 0.15).filter(ImageFilter.GaussianBlur(0.8))
    return img.rotate(1.5, expand=True, fillcolor=255, resample=Image.Resampling.BICUBIC)


def _flat(doc: Document) -> str:
    return " ".join(ln.text for ln in doc.lines).lower()


def _resume3() -> Document:
    path = next(Path(DATA_DIR or "").glob("*резюме 3*.docx"), None)
    if path is None:
        pytest.skip("нет резюме 3 в RVM_DATA_DIR")
    return load_path(path)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@needs_ocr
@needs_data
def test_clean_scan_pdf_of_resume() -> None:
    src = _resume3()
    pages = _pages([ln.text for ln in src.lines])
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=300)
    doc = load_bytes("scan.pdf", buf.getvalue())
    assert doc.status == FileStatus.OK, doc.warnings
    assert doc.lines and all(ln.ocr for ln in doc.lines)
    assert fuzz.ratio(_flat(src), _flat(doc)) / 100 >= 0.9


@needs_ocr
@needs_data
def test_noisy_photo_of_resume() -> None:
    src = _resume3()
    page = _pages([ln.text for ln in src.lines])[0]
    doc = load_bytes("photo.png", _png(_noisy(page)))
    assert doc.lines and all(ln.ocr for ln in doc.lines)
    ref = load_bytes("clean.png", _png(page))
    assert fuzz.ratio(_flat(ref), _flat(doc)) / 100 >= 0.85


@needs_ocr
def test_jpg_photo() -> None:
    img = _pages(["Опыт работы — 3 года", "Python, PostgreSQL, Docker"])[0]
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    doc = load_bytes("photo.jpg", buf.getvalue())
    assert doc.status == FileStatus.OK
    assert "postgresql" in _flat(doc) and doc.lines[0].block == "Опыт работы"


@needs_ocr
def test_low_quality_scan_flagged() -> None:
    text = ["Опыт работы в компании Ромашка", "Разработка сервисов на Python и Go"] * 20
    img = _pages(text)[0]
    small = img.resize((_W // 4, _H // 4)).filter(ImageFilter.GaussianBlur(1.5))
    doc = load_bytes("bad.png", _png(small))
    assert doc.status == FileStatus.SCAN_LOW_QUALITY
    assert doc.lines and "проверьте вручную" in " ".join(doc.warnings)


def test_blank_image_is_empty() -> None:
    doc = load_bytes("blank.png", _png(Image.new("L", (400, 400), 255)))
    assert doc.status == FileStatus.EMPTY and "no_text_layer" in doc.warnings


def test_broken_image() -> None:
    doc = load_bytes("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    assert doc.status == FileStatus.BROKEN


def test_no_rus_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
    doc = load_bytes("blank.png", _png(Image.new("L", (400, 400), 255)))
    assert doc.status == FileStatus.EMPTY and not doc.lines
    assert any("rus" in w or "Tesseract" in w for w in doc.warnings)


def test_no_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "/nonexistent/tesseract")
    doc = load_bytes("blank.png", _png(Image.new("L", (400, 400), 255)))
    assert doc.status == FileStatus.EMPTY
    assert any("Tesseract" in w for w in doc.warnings)


# --- лимиты OCR ---


def _no_tesseract_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_a: object, **_k: object) -> None:
        raise AssertionError("распознавание не должно запускаться")

    monkeypatch.setattr(ocr.pytesseract, "image_to_data", fail)


def _png1(size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("1", size, 1).save(buf, format="PNG")
    return buf.getvalue()


def test_image_over_pixel_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_tesseract_call(monkeypatch)
    doc = load_bytes("big.png", _png1((7000, 7000)))
    assert doc.status == FileStatus.TOO_LARGE and not doc.lines


def test_image_decompression_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_tesseract_call(monkeypatch)
    data = _png1((20000, 20000))
    assert len(data) < 1024 * 1024
    doc = load_bytes("bomb.png", data)
    assert doc.status == FileStatus.BROKEN and not doc.lines
    assert "пиксел" in doc.warnings[0]


def _blank_pdf(pages: int, side: float = 200) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=side, height=side)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_scan_pdf_too_many_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_tesseract_call(monkeypatch)
    doc = load_bytes("scan.pdf", _blank_pdf(11))
    assert doc.status == FileStatus.TOO_LARGE


def test_pdf_page_render_within_budget() -> None:
    pdf = ocr.pdfium.PdfDocument(_blank_pdf(1, side=1700))  # 1700 pt при 300 dpi ≈ 50 МП
    try:
        (img,) = list(ocr._render(pdf))
    finally:
        pdf.close()
    assert img.width * img.height <= ocr.MAX_PIXELS


@needs_ocr
def test_tesseract_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def slow(*_a: object, **kw: Any) -> None:
        seen.update(kw)
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(ocr.pytesseract, "image_to_data", slow)
    doc = load_bytes("blank.png", _png(Image.new("L", (400, 400), 255)))
    assert doc.status == FileStatus.TOO_LARGE and 0 < seen["timeout"] <= ocr.OCR_PAGE_SECONDS
