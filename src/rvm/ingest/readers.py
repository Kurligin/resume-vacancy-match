"""Определение типа файла по сигнатуре и извлечение текста."""

import io
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import docx
import lxml.html
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from striprtf.striprtf import rtf_to_text

from rvm.ingest import limits, ocr
from rvm.ingest.normalize import to_lines
from rvm.models import Document, FileStatus

MAX_BYTES = limits.MAX_BYTES
NO_TEXT_LAYER = ocr.NO_TEXT_LAYER
HIDDEN_TEXT = "в документе скрытый текст (невидимый, белый или очень мелкий шрифт)"

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_ODT_TEXT = "application/vnd.oasis.opendocument.text"
_ODF_T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_HTML_START = re.compile(
    r"\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body)\b",
    re.IGNORECASE | re.DOTALL,
)
_HTML_BLOCKS = (
    "p",
    "div",
    "br",
    "li",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "dt",
    "dd",
    "table",
    "ul",
    "ol",
)
# Картинки, которые отдаём в OCR; HEIF/HEIC открывает pillow-heif. Надёжные сигнатуры
# (битый файл с ними → BROKEN); BMP и HEIC узнаём, только если Pillow их открыл.
_IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"II*\x00", b"MM\x00*")
_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP", "TIFF", "BMP", "GIF", "HEIF"]
# Текстовый слой PDF: меньше этой доли букв алфавита или меньше этого числа букв на странице →
# страница считается сканом (или битым ToUnicode) и уходит в OCR.
MIN_LETTER_SHARE = 0.5
MIN_PAGE_LETTERS = 20
_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")


class _FileError(Exception):
    """Файл не прочитан по понятной причине: статус и сообщение для рекрутера."""

    def __init__(self, status: FileStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_path(path: str | Path) -> Document:
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        return _fail(path.name, FileStatus.TOO_LARGE, f"файл больше {MAX_BYTES // 1024 // 1024} МБ")
    return load_bytes(path.name, path.read_bytes())


def load_text(name: str, text: str) -> Document:
    lines = to_lines(text.splitlines())
    doc = Document(name=name, lines=lines, status=FileStatus.OK if lines else FileStatus.EMPTY)
    return _limit_text(doc)


def load_bytes(name: str, data: bytes) -> Document:
    """Любые байты → Document; исключения наружу не выходят."""
    if len(data) > MAX_BYTES:
        return _fail(name, FileStatus.TOO_LARGE, f"файл больше {MAX_BYTES // 1024 // 1024} МБ")
    try:
        doc = _dispatch(name, data)
    except _FileError as e:
        return _fail(name, e.status, str(e))
    except Exception as e:  # noqa: BLE001 — битый файл любого формата не должен ронять конвейер
        return _fail(name, FileStatus.BROKEN, f"файл повреждён: {type(e).__name__}: {e}")
    if not doc.lines and doc.status == FileStatus.OK:
        doc.status = FileStatus.EMPTY
    return _limit_text(doc)


def _limit_text(doc: Document) -> Document:
    """Слишком длинный текст в модель не отдаём: это и деньги, и время."""
    chars = sum(len(ln.text) for ln in doc.lines)
    if len(doc.lines) > limits.MAX_LINES or chars > limits.MAX_CHARS:
        return _fail(
            doc.name,
            FileStatus.TOO_LARGE,
            f"слишком длинный текст: {len(doc.lines)} строк, {chars} символов "
            f"(не больше {limits.MAX_LINES} строк и {limits.MAX_CHARS} символов)",
        )
    return doc


def check_zip(z: zipfile.ZipFile) -> None:
    """Zip-бомба → BROKEN до распаковки.

    zipfile не отдаёт больше заявленного file_size, поэтому сумма заявленных размеров — честная
    верхняя граница.
    """
    infos = z.infolist()
    unzipped = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos)
    if unzipped > limits.MAX_UNZIPPED_BYTES:
        raise _FileError(
            FileStatus.BROKEN,
            f"архив документа распаковывается больше чем в {limits.MAX_UNZIPPED_BYTES // 2**20} МБ",
        )
    if unzipped > limits.MAX_ZIP_RATIO * max(packed, 1):
        raise _FileError(
            FileStatus.BROKEN, "архив документа сжат подозрительно сильно (похоже на zip-бомбу)"
        )


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
    if data.startswith(_IMAGE_SIGNATURES) or data[8:12] == b"WEBP" or _is_image(data):
        return ocr.read_image(name, data)
    text = _decode_text(data)
    if _HTML_START.match(text[:4096]):
        return _read_html(name, text)
    return load_text(name, text)


def _is_image(data: bytes) -> bool:
    """Картинку определяет сам Pillow по заголовку; текст «BMW…» с подписью BMP — не картинка."""
    try:
        Image.open(io.BytesIO(data), formats=_IMAGE_FORMATS).close()
    except Image.DecompressionBombError:
        return True  # отказ с понятной причиной даст ocr.read_image
    except Exception:  # noqa: BLE001 — любой отказ Pillow значит «не картинка»
        return False
    return True


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise _FileError(FileStatus.BROKEN, "текст в UTF-16 повреждён") from None
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
        names = z.namelist()
        if "word/document.xml" not in names:
            if "mimetype" in names and "content.xml" in names:
                return _read_odt(name, z)
            raise _FileError(FileStatus.UNSUPPORTED, "zip-архив, но не документ Word")
        check_zip(z)
    d = docx.Document(io.BytesIO(data))
    body: list[str] = []
    _walk(d.element.body, body)
    footers: list[str] = []
    roots = [d.element.body]
    seen: set[str] = set()
    for rel in d.part.rels.values():
        if rel.reltype in (RT.HEADER, RT.FOOTER) and rel.target_ref not in seen:
            seen.add(rel.target_ref)
            roots.append(rel.target_part.element)
            _walk(rel.target_part.element, footers)
    hidden = any(_run_look(r) for root in roots for r in root.iter(_W + "r") if _run_text(r))
    return Document(
        name=name, lines=to_lines(body, footers), warnings=[HIDDEN_TEXT] if hidden else []
    )


def _on(el: etree._Element | None) -> bool:
    """Включённый флажок rPr: <w:vanish/> или val не 0/false/off."""
    return el is not None and el.get(_W + "val", "true").lower() not in ("0", "false", "off")


def _run_look(r: etree._Element) -> str | None:
    """«hidden» — run не показывается; «faint» — белый или мельче 4 pt; None — обычный.

    shortcut: смотрим только прямое оформление run, не стили (rStyle) — набивку через стиль
    не поймаем; доработать, если встретится.
    """
    rpr = r.find(_W + "rPr")
    if rpr is None:
        return None
    if _on(rpr.find(_W + "vanish")) or _on(rpr.find(_W + "webHidden")):
        return "hidden"
    color = rpr.find(_W + "color")
    size = rpr.find(_W + "sz")
    if color is not None and (color.get(_W + "val") or "").upper() == "FFFFFF":
        return "faint"
    half_points = (size.get(_W + "val") or "") if size is not None else ""
    if half_points.isdigit() and int(half_points) < 8:
        return "faint"  # меньше 4 pt
    return None


def _run_text(r: etree._Element) -> bool:
    return any((t.text or "").strip() for t in r.iter(_W + "t"))


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
        parent = el.getparent()
        if parent is not None and parent.tag == _W + "r" and _run_look(parent) == "hidden":
            continue  # невидимый run: набивка ключевыми словами не должна стать цитатой
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


# --- odt, html ---


def _read_odt(name: str, z: zipfile.ZipFile) -> Document:
    check_zip(z)
    if z.read("mimetype")[:100].decode("ascii", "replace").strip() != _ODT_TEXT:
        raise _FileError(FileStatus.UNSUPPORTED, "документ OpenDocument, но не текстовый (.odt)")
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(z.read("content.xml"), parser)
    paras = [
        _odt_text(el)
        for el in root.iter(_ODF_T + "p", _ODF_T + "h")
        if not any(a.tag in (_ODF_T + "p", _ODF_T + "h") for a in el.iterancestors())
    ]
    return Document(name=name, lines=to_lines(paras))


def _odt_text(el: etree._Element) -> str:
    """Текст абзаца ODF: <text:s/> — пробелы, <text:tab/> — пробел, <text:line-break/> — строка."""
    parts = [el.text or ""]
    for child in el:
        tag = child.tag
        if tag == _ODF_T + "s":
            parts.append(" ")  # подряд идущие пробелы всё равно схлопнет нормализация
        elif tag == _ODF_T + "tab":
            parts.append(" ")
        elif tag == _ODF_T + "line-break":
            parts.append("\n")
        elif tag not in (_ODF_T + "note", _ODF_T + "annotation"):
            parts.append(_odt_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _read_html(name: str, text: str) -> Document:
    text = re.sub(r"^\s*<\?xml[^>]*>", "", text)  # lxml не принимает str с объявлением кодировки
    root = lxml.html.document_fromstring(text)
    for el in list(root.iter("script", "style", "noscript", "template", "head")):
        el.drop_tree()
    for el in root.iter(*_HTML_BLOCKS):
        el.tail = "\n" + (el.tail or "")
        if el.tag == "br":
            continue
        el.text = "\n" + (el.text or "")
    return load_text(name, root.text_content())


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
    if len(reader.pages) > limits.MAX_PDF_PAGES:
        raise _FileError(FileStatus.TOO_LARGE, f"в PDF больше {limits.MAX_PDF_PAGES} страниц")
    # shortcut: время проверяется между страницами, зависшую страницу не прервёт;
    # нужен отдельный процесс, если такие PDF появятся.
    deadline = time.monotonic() + limits.PDF_SECONDS
    pages: list[str] = []
    for page in reader.pages:
        if time.monotonic() > deadline:
            raise _FileError(FileStatus.TOO_LARGE, "PDF читается слишком долго")
        text = page.extract_text() or ""
        pages.append(text if _readable(text) else "")
    if all(pages):
        return Document(name=name, lines=to_lines(pages))
    return ocr.read_pdf(name, data, pages)  # страницы без читаемого текста — в OCR


def _readable(text: str) -> bool:
    """Текстовый слой читаем: достаточно букв, и они — большая часть непробельных символов."""
    letters = len(_LETTER.findall(text))
    visible = len("".join(text.split()))
    return letters >= MIN_PAGE_LETTERS and letters >= MIN_LETTER_SHARE * visible


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
