"""Тесты чтения файлов и нормализации."""

import io
import os
import time
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from pypdf import PdfWriter

from rvm.ingest import load_bytes, load_path, load_text
from rvm.ingest.normalize import header_block, normalize_line
from rvm.ingest.readers import HIDDEN_TEXT
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
        assert d.status == FileStatus.OK and not d.warnings, name
        assert d.lines and all(ln.text for ln in d.lines), name
    full = "\n".join(ln.text for d in docs.values() for ln in d.lines)
    assert "Frontend-разработчик" in full and "utility-классов" in full
    assert "Frontendразработчик" not in full
    r3 = next(d for n, d in docs.items() if "резюме 3" in n)
    assert any("(2 года)" in ln.text for ln in r3.lines)
    assert any(ln.block == "Опыт работы" for ln in r3.lines)
    blocks = {ln.text: ln.block for ln in r3.lines}
    assert blocks["Дополнительно"] == "Дополнительная информация"
    assert (
        blocks["Не готов к переезду, готов к командировкам"] is None
    )  # «Гражданство: …» — не заголовок


# --- лимиты и безопасность ---


def _bomb_docx(mb: int) -> bytes:
    """Настоящий docx, в document.xml которого перед </w:body> вставлены mb мегабайт пробелов."""
    src = zipfile.ZipFile(io.BytesIO(_docx_bytes(DocxDocument())))
    xml = src.read("word/document.xml")
    cut = xml.index(b"</w:body>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename != "word/document.xml":
                z.writestr(info.filename, src.read(info.filename))
        with z.open("word/document.xml", "w") as f:
            f.write(xml[:cut])
            chunk = b" " * (1024 * 1024)
            for _ in range(mb):
                f.write(chunk)
            f.write(xml[cut:])
    return buf.getvalue()


@pytest.mark.parametrize("mb", [60, 5])  # 60 МБ — по объёму, 5 МБ — по коэффициенту сжатия
def test_docx_zip_bomb(mb: int) -> None:
    data = _bomb_docx(mb)
    assert len(data) < 1024 * 1024
    tracemalloc.start()
    t = time.monotonic()
    d = load_bytes("bomb.docx", data)
    elapsed = time.monotonic() - t
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert d.status == FileStatus.BROKEN and "архив" in d.warnings[0]
    assert elapsed < 1 and peak < 5 * 1024 * 1024


def test_text_too_many_lines() -> None:
    text = "\n".join(f"строка {i}" for i in range(3000))
    assert load_bytes("a.txt", text.encode()).status == FileStatus.TOO_LARGE
    assert load_text("a", text).status == FileStatus.TOO_LARGE


def test_text_too_many_chars() -> None:
    d = load_text("a", "\n".join("слово " * 20 for _ in range(700)))
    assert d.status == FileStatus.TOO_LARGE and not d.lines


def test_pdf_too_many_pages() -> None:
    w = PdfWriter()
    for _ in range(21):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    assert load_bytes("big.pdf", buf.getvalue()).status == FileStatus.TOO_LARGE


def _run(text: str, rpr: str = "") -> object:
    return parse_xml(
        f"<w:r {nsdecls('w')}><w:rPr>{rpr}</w:rPr><w:t xml:space='preserve'>{text}</w:t></w:r>"
    )


def _hidden_docx(rpr: str) -> bytes:
    doc = DocxDocument()
    p = doc.add_paragraph()
    p._p.append(_run("React "))
    p._p.append(_run("Vue 3, Tailwind", rpr))
    return _docx_bytes(doc)


@pytest.mark.parametrize("rpr", ["<w:vanish/>", "<w:webHidden/>", "<w:vanish w:val='true'/>"])
def test_docx_hidden_run_skipped(rpr: str) -> None:
    d = load_bytes("a.docx", _hidden_docx(rpr))
    full = " ".join(ln.text for ln in d.lines)
    assert "React" in full and "Vue" not in full and "Tailwind" not in full
    assert HIDDEN_TEXT in d.warnings


@pytest.mark.parametrize("rpr", ["<w:color w:val='FFFFFF'/>", "<w:sz w:val='6'/>"])
def test_docx_faint_text_warned(rpr: str) -> None:
    d = load_bytes("a.docx", _hidden_docx(rpr))
    assert any("Vue 3, Tailwind" in ln.text for ln in d.lines)
    assert d.status == FileStatus.OK and HIDDEN_TEXT in d.warnings


@pytest.mark.parametrize(
    "rpr", ["<w:vanish w:val='0'/>", "<w:color w:val='000000'/><w:sz w:val='8'/>"]
)
def test_docx_visible_text_no_warning(rpr: str) -> None:
    d = load_bytes("a.docx", _hidden_docx(rpr))
    assert any("Vue 3, Tailwind" in ln.text for ln in d.lines)
    assert HIDDEN_TEXT not in d.warnings


# --- форматы и заголовки блоков (R1f) ---


def test_html_without_tags_and_scripts() -> None:
    html = (
        "<!DOCTYPE html><html><head><title>Резюме</title><style>p{color:red}</style>"
        "<script>var x = 'Python';</script></head><body><h2>Опыт работы</h2>"
        "<p>ООО <b>Ромашка</b>, разработчик</p><ul><li>Vue 3</li><li>Tailwind CSS</li></ul>"
        "<p>строка<br>с переносом &amp; сущностью</p></body></html>"
    )
    d = load_bytes("r.html", html.encode("utf-8"))
    assert d.status == FileStatus.OK
    assert [ln.text for ln in d.lines] == [
        "Опыт работы",
        "ООО Ромашка, разработчик",
        "Vue 3",
        "Tailwind CSS",
        "строка",
        "с переносом & сущностью",
    ]
    assert d.lines[1].block == "Опыт работы"


def _odt(content: str, mimetype: str = "application/vnd.oasis.opendocument.text") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", mimetype)
        z.writestr("content.xml", content)
    return buf.getvalue()


_ODT_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
)


def test_odt() -> None:
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_ODT_NS}>'
        "<office:body><office:text><text:h>Опыт работы</text:h>"
        "<text:p>ООО<text:s/>Ромашка,<text:tab/>Python<text:line-break/>Go</text:p>"
        "<table:table><table:table-row><table:table-cell><text:p>2020 — 2023</text:p>"
        "</table:table-cell></table:table-row></table:table>"
        "</office:text></office:body></office:document-content>"
    )
    d = load_bytes("r.odt", _odt(content))
    assert d.status == FileStatus.OK
    assert [(ln.text, ln.block) for ln in d.lines] == [
        ("Опыт работы", "Опыт работы"),
        ("ООО Ромашка, Python", "Опыт работы"),
        ("Go", "Опыт работы"),
        ("2020 — 2023", "Опыт работы"),
    ]


def test_odt_spreadsheet_unsupported() -> None:
    d = load_bytes("t.ods", _odt("<x/>", "application/vnd.oasis.opendocument.spreadsheet"))
    assert d.status == FileStatus.UNSUPPORTED


def test_odt_zip_bomb() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", b" " * (60 * 2**20))
    assert load_bytes("b.odt", buf.getvalue()).status == FileStatus.BROKEN


@pytest.mark.parametrize("enc", ["utf-16", "utf-16-be"])
def test_txt_utf16_bom(enc: str) -> None:
    data = "Навыки\nPython, SQL".encode(enc)
    if enc == "utf-16-be":
        data = b"\xfe\xff" + data
    d = load_bytes("r.txt", data)
    assert [(ln.text, ln.block) for ln in d.lines] == [
        ("Навыки", "Навыки"),
        ("Python, SQL", "Навыки"),
    ]


@pytest.mark.parametrize(
    ("line", "block"),
    [
        ("О себе", "Обо мне"),
        ("ОБО МНЕ:", "Обо мне"),
        ("About me", "Обо мне"),
        ("Summary", "Обо мне"),
        ("Личные качества", "Обо мне"),
        ("Skills", "Навыки"),
        ("Профессиональные навыки", "Навыки"),
        ("Опыт", "Опыт работы"),
        ("Experience", "Опыт работы"),
        ("Work experience — 5 years", "Опыт работы"),
        ("Education", "Образование"),
        ("Дополнительно", "Дополнительная информация"),
        ("Дополнительная информация:", "Дополнительная информация"),
        ("Гражданство, время в пути до работы", "Гражданство"),
        ("Желаемая должность и зарплата", "Желаемая должность"),
        ("Опыт работы — 7 лет 9 месяцев", "Опыт работы"),
    ],
)
def test_header_aliases(line: str, block: str) -> None:
    assert header_block(line) == block


@pytest.mark.parametrize(
    "line",
    [
        "Гражданство: Россия",
        "Гражданство: Россия, есть разрешение на работу: Россия",
        "Навыки: Python, SQL",
        "Образование: высшее экономическое.",
        "Опыт работы с Git",
        "Опыт работы - в команде",
        "Дополнительно (плюс): понимание различий SPA и SSR",
        "Skills: Python",
    ],
)
def test_not_headers(line: str) -> None:
    assert header_block(line) is None


def test_inline_label_keeps_block() -> None:
    d = load_text("r", "О себе\nОтветственный\nГражданство: Россия\nSkills\nPython")
    assert [ln.block for ln in d.lines] == ["Обо мне", "Обо мне", "Обо мне", "Навыки", "Навыки"]
