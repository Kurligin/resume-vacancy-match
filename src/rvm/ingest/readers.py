"""Определение типа файла по сигнатуре и извлечение текста."""

import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import docx
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from striprtf.striprtf import rtf_to_text

from rvm.ingest import ocr
from rvm.ingest.normalize import to_lines
from rvm.models import Document, FileStatus

MAX_BYTES = 15 * 1024 * 1024
NO_TEXT_LAYER = ocr.NO_TEXT_LAYER

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


class _FileError(Exception):
    """Файл не прочитан по понятной причине: статус и сообщение для рекрутера."""

    def __init__(self, status: FileStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_path(path: str | Path) -> Document:
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        return _fail(path.name, FileStatus.TOO_LARGE, "файл больше 15 МБ")
    return load_bytes(path.name, path.read_bytes())


def load_text(name: str, text: str) -> Document:
    lines = to_lines(text.splitlines())
    return Document(name=name, lines=lines, status=FileStatus.OK if lines else FileStatus.EMPTY)


def load_bytes(name: str, data: bytes) -> Document:
    """Любые байты → Document; исключения наружу не выходят."""
    if len(data) > MAX_BYTES:
        return _fail(name, FileStatus.TOO_LARGE, "файл больше 15 МБ")
    try:
        doc = _dispatch(name, data)
    except _FileError as e:
        return _fail(name, e.status, str(e))
    except Exception as e:  # noqa: BLE001 — битый файл любого формата не должен ронять конвейер
        return _fail(name, FileStatus.BROKEN, f"файл повреждён: {type(e).__name__}: {e}")
    if not doc.lines and doc.status == FileStatus.OK:
        doc.status = FileStatus.EMPTY
    return doc


def _fail(name: str, status: FileStatus, message: str) -> Document:
    return Document(name=name, lines=[], status=status, warnings=[message])


def _dispatch(name: str, data: bytes) -> Document:
    if data.startswith(b"PK\x03\x04"):
        return _read_docx(name, data)
    if b"%PDF" in data[:1024]:
        return _read_pdf(name, data)
    if data.lstrip().startswith(b"{\\rtf"):
        return _read_rtf(name, data)
    if data.startswith(_OLE):
        if "EncryptedPackage".encode("utf-16-le") in data:
            raise _FileError(FileStatus.ENCRYPTED, "документ защищён паролем")
        return _read_doc(name, data)
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        return ocr.read_image(name, data)
    return load_text(name, _decode_text(data))


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        controls = sum(1 for c in text if ord(c) < 32 and c not in "\t\r\n\f")
        if controls <= len(text) * 0.01:
            return text
    raise _FileError(FileStatus.UNSUPPORTED, "неизвестный формат файла")


# --- docx ---


def _read_docx(name: str, data: bytes) -> Document:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            raise _FileError(FileStatus.UNSUPPORTED, "zip-архив, но не документ Word")
    d = docx.Document(io.BytesIO(data))
    body: list[str] = []
    _walk(d.element.body, body)
    footers: list[str] = []
    seen: set[str] = set()
    for rel in d.part.rels.values():
        if rel.reltype in (RT.HEADER, RT.FOOTER) and rel.target_ref not in seen:
            seen.add(rel.target_ref)
            _walk(rel.target_part.element, footers)
    return Document(name=name, lines=to_lines(body, footers))


def _walk(container: etree._Element, out: list[str]) -> None:
    """Абзацы и таблицы (вложенные тоже) по порядку документа."""
    for el in container:
        if el.tag == _W + "p":
            out.append(_paragraph_text(el))
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                if _ancestor_table(tr) is not el:
                    continue  # строки вложенных таблиц обойдёт рекурсия по ячейке
                for tc in tr.findall(_W + "tc"):
                    vmerge = tc.find(f"{_W}tcPr/{_W}vMerge")
                    if vmerge is not None and vmerge.get(_W + "val") in (None, "continue"):
                        continue  # продолжение вертикально объединённой ячейки
                    _walk(tc, out)
        elif el.tag in (_W + "sdt", _W + "sdtContent", _W + "customXml", _W + "hdr", _W + "ftr"):
            _walk(el, out)


def _ancestor_table(el: etree._Element) -> etree._Element | None:
    parent = el.getparent()
    while parent is not None and parent.tag != _W + "tbl":
        parent = parent.getparent()
    return parent


def _paragraph_text(p: etree._Element) -> str:
    parts: list[str] = []
    for el in p.iter():
        if any(a.tag == _MC_FALLBACK for a in el.iterancestors()):
            continue  # запасная копия надписи, иначе текст задвоится
        tag = el.tag
        if tag == _W + "t":
            parts.append(el.text or "")
        elif tag == _W + "noBreakHyphen":
            parts.append("-")
        elif tag in (_W + "tab", _W + "ptab"):
            parts.append(" ")
        elif tag in (_W + "br", _W + "cr") or (tag == _W + "p" and el is not p):
            parts.append("\n")
    return "".join(parts)


# --- pdf, rtf, doc ---


def _read_pdf(name: str, data: bytes) -> Document:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except (PyPdfError, NotImplementedError):
            ok = 0
        if not ok:
            raise _FileError(FileStatus.ENCRYPTED, "PDF защищён паролем")
    pages = [page.extract_text() or "" for page in reader.pages]
    lines = to_lines(pages)
    if not lines:
        return ocr.read_pdf(name, data)
    return Document(name=name, lines=lines)


def _read_rtf(name: str, data: bytes) -> Document:
    head = data[:2048].decode("latin-1")
    cp = re.search(r"\\ansicpg(\d+)", head)
    text = rtf_to_text(data.decode("latin-1"), encoding=f"cp{cp.group(1)}" if cp else "cp1251")
    return load_text(name, text)


def _read_doc(name: str, data: bytes) -> Document:
    exe = shutil.which("antiword")
    if exe is None:
        raise _FileError(FileStatus.UNSUPPORTED, "формат .doc: не установлен antiword")
    with tempfile.NamedTemporaryFile(suffix=".doc") as tmp:
        tmp.write(data)
        tmp.flush()
        res = subprocess.run(
            [exe, "-m", "UTF-8.txt", tmp.name], capture_output=True, timeout=60, check=False
        )
    if res.returncode != 0:
        raise _FileError(FileStatus.BROKEN, "antiword не смог прочитать .doc")
    return load_text(name, res.stdout.decode("utf-8", errors="replace"))
