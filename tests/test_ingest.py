"""Тесты чтения файлов и нормализации."""

import io
import os
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from pypdf import PdfWriter

from rvm.ingest import load_bytes, load_path, load_text
from rvm.ingest.normalize import normalize_line
from rvm.models import FileStatus

DATA_DIR = os.environ.get("RVM_DATA_DIR")
needs_data = pytest.mark.skipif(not DATA_DIR, reason="нет RVM_DATA_DIR")


def _docx_bytes(doc: object) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)  # type: ignore[attr-defined]
    return buf.getvalue()


def _sample_docx() -> bytes:
    doc = DocxDocument()
    doc.add_paragraph("Опыт работы — 3 года")
    p = doc.add_paragraph()
    p._p.append(parse_xml(f"<w:r {nsdecls('w')}><w:t>Frontend</w:t></w:r>"))
    p._p.append(parse_xml(f"<w:r {nsdecls('w')}><w:noBreakHyphen/></w:r>"))
    p._p.append(parse_xml(f"<w:r {nsdecls('w')}><w:t>разработчик</w:t></w:r>"))
    p._p.append(
        parse_xml(f"<w:r {nsdecls('w')}><w:t>пере</w:t><w:softHyphen/><w:t>нос</w:t></w:r>")
    )
    table = doc.add_table(rows=2, cols=2)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.text = "Объединённая ячейка"
    table.cell(1, 0).text = "2020 — 2023"
    inner = table.cell(1, 1).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "Вложенная таблица"
    doc.add_paragraph("Образование")
    doc.add_paragraph("МФТИ")
    doc.sections[0].header.paragraphs[0].text = "Резюме обновлено 1 ноября"
    return _docx_bytes(doc)


def test_docx_table_hyphen_header() -> None:
    d = load_bytes("a.docx", _sample_docx())
    assert d.status == FileStatus.OK
    texts = [ln.text for ln in d.lines]
    assert "Frontend-разработчикперенос" in texts
    assert texts.count("Объединённая ячейка") == 1
    assert "Вложенная таблица" in texts
    assert texts.index("2020 — 2023") < texts.index("Вложенная таблица") < texts.index("МФТИ")
    assert [ln.n for ln in d.lines] == list(range(1, len(d.lines) + 1))
    last = d.lines[-1]
    assert last.text == "Резюме обновлено 1 ноября" and last.block == "колонтитул"
    blocks = {ln.text: ln.block for ln in d.lines}
    assert blocks["Опыт работы — 3 года"] == "Опыт работы"
    assert blocks["Вложенная таблица"] == "Опыт работы"
    assert blocks["МФТИ"] == "Образование"


def test_empty_docx() -> None:
    d = load_bytes("e.docx", _docx_bytes(DocxDocument()))
    assert d.status == FileStatus.EMPTY and d.lines == []


def test_broken_zip() -> None:
    d = load_bytes("b.docx", b"PK\x03\x04" + b"\x00garbage" * 20)
    assert d.status == FileStatus.BROKEN and d.warnings


def test_encrypted_ole() -> None:
    data = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 500 + "EncryptedPackage".encode("utf-16-le")
    )
    assert load_bytes("x.docx", data).status == FileStatus.ENCRYPTED


def test_doc_without_antiword(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rvm.ingest.readers.shutil.which", lambda _: None)
    d = load_bytes("x.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 500)
    assert d.status == FileStatus.UNSUPPORTED and "antiword" in d.warnings[0]


def test_pdf_without_text() -> None:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    d = load_bytes("scan.pdf", buf.getvalue())
    assert d.status == FileStatus.EMPTY and "no_text_layer" in d.warnings


def test_pdf_encrypted() -> None:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.encrypt("secret", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    assert load_bytes("p.pdf", buf.getvalue()).status == FileStatus.ENCRYPTED


def test_rtf() -> None:
    rtf = rb"{\rtf1\ansi\ansicpg1251 \'ce\'ef\'fb\'f2 \'f0\'e0\'e1\'ee\'f2\'fb\par Python\par}"
    d = load_bytes("r.rtf", rtf)
    assert [ln.text for ln in d.lines] == ["Опыт работы", "Python"]


def test_txt_cp1251() -> None:
    d = load_bytes("r.txt", "Ключевые навыки\nPython,\u00a0SQL\n\n".encode("cp1251"))
    assert d.status == FileStatus.OK
    assert [(ln.text, ln.block) for ln in d.lines] == [
        ("Ключевые навыки", "Ключевые навыки"),
        ("Python, SQL", "Ключевые навыки"),
    ]


def test_binary_is_unsupported() -> None:
    assert load_bytes("x.bin", bytes(range(256)) * 4).status == FileStatus.UNSUPPORTED


def test_too_large() -> None:
    assert load_bytes("big.txt", b"a" * (15 * 1024 * 1024 + 1)).status == FileStatus.TOO_LARGE


def test_normalize_line() -> None:
    assert (
        normalize_line("Frontend\u2011разработчик\u00a0 и  UI\u2013kit")
        == "Frontend-разработчик и UI-kit"
    )
    assert normalize_line("пере\u00adнос\u200b") == "перенос"
    assert normalize_line("2019 — 2021") == "2019 — 2021"


def test_blocks_and_tail() -> None:
    d = load_text(
        "v", "Требования\nPython\nОПЫТ РАБОТЫ — 7 лет 9 месяцев\nООО Ромашка\nНавыки работы с Git"
    )
    assert [ln.block for ln in d.lines] == [None, None, "Опыт работы", "Опыт работы", "Опыт работы"]
    assert load_text("e", "  \n ").status == FileStatus.EMPTY


@needs_data
def test_case_files() -> None:
    files = sorted(Path(DATA_DIR or "").glob("*.docx"))
    assert len(files) == 7
    docs = {f.name: load_path(f) for f in files}
    for name, d in docs.items():
        assert d.status == FileStatus.OK, name
        assert d.lines and all(ln.text for ln in d.lines), name
    full = "\n".join(ln.text for d in docs.values() for ln in d.lines)
    assert "Frontend-разработчик" in full and "utility-классов" in full
    assert "Frontendразработчик" not in full
    r3 = next(d for n, d in docs.items() if "резюме 3" in n)
    assert any("(2 года)" in ln.text for ln in r3.lines)
    assert any(ln.block == "Опыт работы" for ln in r3.lines)
