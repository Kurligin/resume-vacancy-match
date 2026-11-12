"""Безопасность чтения: белый список картинок (без Ghostscript) и HTML без отката."""

import io
import time

import pytest
from PIL import EpsImagePlugin, Image

from rvm.ingest import load_bytes, ocr
from rvm.models import FileStatus


def _eps(binary: bool) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (20, 20), 255).save(buf, format="EPS")
    ps = buf.getvalue()
    if not binary:
        return ps
    # двоичный заголовок DOS EPS: сигнатура, смещение и длина PostScript
    head = b"\xc5\xd0\xd3\xc6" + (30).to_bytes(4, "little") + len(ps).to_bytes(4, "little")
    return head + b"\x00" * 18 + ps


@pytest.fixture
def no_ghostscript(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def fake(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise AssertionError("Ghostscript вызван")

    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", fake)
    return calls


@pytest.mark.parametrize("binary", [False, True])
def test_eps_disguised_as_image_refused(no_ghostscript: list[int], binary: bool) -> None:
    doc = ocr.read_image("photo.png", _eps(binary))
    assert doc.status == FileStatus.BROKEN and not doc.lines
    doc = load_bytes("photo.png", _eps(binary))
    assert not any(ln.ocr for ln in doc.lines)
    assert not no_ghostscript


def test_webp_signature_strict() -> None:
    doc = load_bytes("r.txt", "12345678WEBP резюме\nPython".encode())
    assert doc.status == FileStatus.OK and doc.lines[0].text == "12345678WEBP резюме"


@pytest.mark.parametrize("head", ["<!--" * 10_000, "<!---->" * 600 + "x", "<!-- --><!--" * 300])
def test_html_comments_fast(head: str) -> None:
    data = (head + "<html><body><p>Python</p></body></html>").encode()
    start = time.perf_counter()
    load_bytes("r.html", data)
    assert time.perf_counter() - start < 0.5


def test_html_after_leading_comments() -> None:
    html = '<?xml version="1.0"?>\n<!-- a -->\n<!-- b --><html><body><p>Python</p></body></html>'
    doc = load_bytes("r.html", html.encode())
    assert [ln.text for ln in doc.lines] == ["Python"]
