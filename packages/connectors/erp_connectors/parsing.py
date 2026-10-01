"""File validation and layout-aware parsing for PDF, DOCX, HTML and plain text/markdown.

Every parser reports per-page/section extraction coverage. Pages without extractable text and embedded
images are *reported*, never silently dropped. Encrypted/protected files raise ``ProtectedContentError``
and are quarantined — this code never decrypts content.
"""

from __future__ import annotations

import io
import re
import statistics
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from erp_rag.schemas import EXTRACTION_VERSION

MediaType = Literal["pdf", "docx", "html", "text"]
MIN_PAGE_CHARS = 20
MAX_PDF_PAGES = 2000
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_DOCX_RATIO = 100


class ValidationFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtectedContentError(ValidationFailure):
    def __init__(self) -> None:
        super().__init__("protected_content")


class Block(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["heading", "paragraph", "table", "list_item"]
    text: str
    level: int = 0
    page: int | None = None
    line: int | None = None
    table_header: list[str] | None = None
    table_rows: list[list[str]] | None = None


class PageCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int
    status: Literal["text", "ocr", "no_text", "failed"]
    chars: int
    images: int = 0
    detail: str | None = None


class ParsedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    media_type: MediaType
    extraction_version: str = EXTRACTION_VERSION
    blocks: list[Block]
    pages: list[PageCoverage]
    unprocessed_images: int = 0
    warnings: list[str] = []

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def covered_pages(self) -> int:
        return sum(1 for p in self.pages if p.status in ("text", "ocr"))

    @property
    def coverage_ratio(self) -> float:
        return self.covered_pages / self.page_count if self.pages else 0.0

    def coverage_report(self) -> dict[str, object]:
        return {
            "extraction_version": self.extraction_version,
            "pages": self.page_count,
            "covered_pages": self.covered_pages,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "uncovered": [p.model_dump() for p in self.pages if p.status not in ("text", "ocr")],
            "unprocessed_images": self.unprocessed_images,
            "warnings": self.warnings,
        }


# OCR hook: (pdf_bytes, page_number_1_based) -> text. Provided by the Textract adapter when configured.
OcrFn = Callable[[bytes, int], Awaitable[str]]


def detect_media_type(data: bytes, filename: str) -> MediaType:
    """Magic bytes must agree with the extension; disagreement fails validation."""
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    head = data[:2048]
    if head.startswith(b"%PDF-"):
        kind: MediaType = "pdf"
    elif head.startswith(b"PK\x03\x04"):
        kind = "docx"
    elif re.search(rb"(?i)<(!doctype html|html|body|head)\b", head):
        kind = "html"
    else:
        try:
            data[:65536].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailure("unsupported_binary") from exc
        kind = "text"
    expected = {"pdf": {"pdf"}, "docx": {"docx"}, "html": {"html", "htm"}, "text": {"txt", "md"}}[kind]
    if suffix not in expected:
        raise ValidationFailure(f"type_mismatch:{kind}:{suffix or 'none'}")
    return kind


def validate_file(data: bytes, filename: str, *, max_bytes: int) -> MediaType:
    if not data:
        raise ValidationFailure("empty_file")
    if len(data) > max_bytes:
        raise ValidationFailure("file_too_large")
    kind = detect_media_type(data, filename)
    if kind == "docx":
        _check_docx_archive(data)
    return kind


def _check_docx_archive(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                raise ValidationFailure("not_a_docx")
            if "EncryptionInfo" in names or "EncryptedPackage" in names:
                raise ProtectedContentError()
            total = sum(i.file_size for i in zf.infolist())
            compressed = sum(i.compress_size for i in zf.infolist()) or 1
            if total > MAX_DOCX_UNCOMPRESSED or total / compressed > MAX_DOCX_RATIO:
                raise ValidationFailure("archive_bomb_suspected")
    except zipfile.BadZipFile as exc:
        raise ValidationFailure("corrupt_archive") from exc


# ---------------------------------------------------------------- text / markdown

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")


def parse_text(data: bytes) -> ParsedDocument:
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    blocks: list[Block] = []
    para: list[str] = []
    para_line = 1
    i = 0

    def flush() -> None:
        nonlocal para
        if para:
            blocks.append(
                Block(kind="paragraph", text=" ".join(s.strip() for s in para), line=para_line, page=None)
            )
            para = []

    while i < len(lines):
        line = lines[i]
        m = _MD_HEADING.match(line)
        if m:
            flush()
            blocks.append(Block(kind="heading", text=m.group(2).strip(), level=len(m.group(1)), line=i + 1))
        elif line.strip().startswith("|") and i + 1 < len(lines) and _MD_TABLE_SEP.match(lines[i + 1]):
            flush()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows: list[list[str]] = []
            start = i + 1
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append(
                Block(
                    kind="table",
                    text=_table_text(header, rows),
                    table_header=header,
                    table_rows=rows,
                    line=start,
                )
            )
            continue
        elif re.match(r"^\s*([-*•]|\d+[.)])\s+", line):
            flush()
            blocks.append(
                Block(kind="list_item", text=re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", line).strip(), line=i + 1)
            )
        elif not line.strip():
            flush()
        else:
            if not para:
                para_line = i + 1
            para.append(line)
        i += 1
    flush()
    chars = sum(len(b.text) for b in blocks)
    status: Literal["text", "no_text"] = "text" if chars >= MIN_PAGE_CHARS else "no_text"
    return ParsedDocument(
        media_type="text", blocks=blocks, pages=[PageCoverage(page=1, status=status, chars=chars)]
    )


def _table_text(header: list[str], rows: list[list[str]]) -> str:
    out = [" | ".join(header)]
    for row in rows:
        cells = [f"{h}: {v}" for h, v in zip(header, row, strict=False) if v]
        out.append("; ".join(cells))
    return "\n".join(out)


# ---------------------------------------------------------------- HTML


def parse_html(data: bytes) -> ParsedDocument:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "iframe", "object"]):
        tag.decompose()
    blocks: list[Block] = []
    images = len(soup.find_all("img"))
    warnings = [f"{images} image(s) not processed (alt text kept when present)"] if images else []
    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre", "img"]):
        if el.find_parent("table") is not None and el.name != "table":
            continue
        if el.name == "img":
            alt = str(el.get("alt") or "").strip()
            if alt:
                blocks.append(Block(kind="paragraph", text=f"[Image: {alt}]"))
            continue
        if el.name == "table":
            rows = [
                [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")
            ]
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            header, body_rows = rows[0], rows[1:]
            blocks.append(
                Block(
                    kind="table",
                    text=_table_text(header, body_rows),
                    table_header=header,
                    table_rows=body_rows,
                )
            )
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name and el.name[0] == "h":
            blocks.append(Block(kind="heading", text=text, level=int(el.name[1])))
        elif el.name == "li":
            blocks.append(Block(kind="list_item", text=text))
        else:
            blocks.append(Block(kind="paragraph", text=text))
    chars = sum(len(b.text) for b in blocks)
    status: Literal["text", "no_text"] = "text" if chars >= MIN_PAGE_CHARS else "no_text"
    return ParsedDocument(
        media_type="html",
        blocks=blocks,
        pages=[PageCoverage(page=1, status=status, chars=chars, images=images)],
        unprocessed_images=images,
        warnings=warnings,
    )


# ---------------------------------------------------------------- DOCX


def parse_docx(data: bytes) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, document)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"(?i)heading (\d)", style)
            if m or style.lower() == "title":
                blocks.append(Block(kind="heading", text=text, level=int(m.group(1)) if m else 1))
            elif "list" in style.lower():
                blocks.append(Block(kind="list_item", text=text))
            else:
                blocks.append(Block(kind="paragraph", text=text))
        elif tag == "tbl":
            t = Table(child, document)
            rows = [[c.text.strip() for c in row.cells] for row in t.rows]
            rows = [r for r in rows if any(r)]
            if rows:
                blocks.append(
                    Block(
                        kind="table",
                        text=_table_text(rows[0], rows[1:]),
                        table_header=rows[0],
                        table_rows=rows[1:],
                    )
                )
    images = len(document.inline_shapes)
    chars = sum(len(b.text) for b in blocks)
    status: Literal["text", "no_text"] = "text" if chars >= MIN_PAGE_CHARS else "no_text"
    return ParsedDocument(
        media_type="docx",
        blocks=blocks,
        pages=[PageCoverage(page=1, status=status, chars=chars, images=images)],
        unprocessed_images=images,
        warnings=[f"{images} embedded image(s) not processed"] if images else [],
    )


# ---------------------------------------------------------------- PDF


async def parse_pdf(data: bytes, *, ocr: OcrFn | None = None) -> ParsedDocument:
    import pdfplumber
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ProtectedContentError()
        n_pages = len(reader.pages)
    except PdfReadError as exc:
        raise ValidationFailure("corrupt_pdf") from exc
    if n_pages > MAX_PDF_PAGES:
        raise ValidationFailure("too_many_pages")

    blocks: list[Block] = []
    pages: list[PageCoverage] = []
    images_total = 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            images = len(page.images)
            images_total += images
            try:
                page_blocks = _pdf_page_blocks(page, number)
            except Exception as exc:  # a broken page is reported, not dropped silently
                pages.append(
                    PageCoverage(
                        page=number, status="failed", chars=0, images=images, detail=type(exc).__name__
                    )
                )
                continue
            chars = sum(len(b.text) for b in page_blocks)
            if chars >= MIN_PAGE_CHARS:
                blocks.extend(page_blocks)
                pages.append(PageCoverage(page=number, status="text", chars=chars, images=images))
                continue
            if ocr is not None:
                try:
                    text = (await ocr(data, number)).strip()
                except Exception as exc:
                    pages.append(
                        PageCoverage(
                            page=number,
                            status="failed",
                            chars=0,
                            images=images,
                            detail=f"ocr:{type(exc).__name__}",
                        )
                    )
                    continue
                if len(text) >= MIN_PAGE_CHARS:
                    blocks.extend(
                        Block(kind="paragraph", text=t, page=number) for t in text.split("\n\n") if t.strip()
                    )
                    pages.append(PageCoverage(page=number, status="ocr", chars=len(text), images=images))
                    continue
            pages.append(
                PageCoverage(
                    page=number,
                    status="no_text",
                    chars=chars,
                    images=images,
                    detail="scanned/image-only page; OCR not configured" if ocr is None else "ocr_empty",
                )
            )
    # Images on pages with text are not OCR'd; report them.
    unprocessed = sum(p.images for p in pages if p.status == "text")
    warnings = [f"{unprocessed} embedded image(s) on text pages not processed"] if unprocessed else []
    return ParsedDocument(
        media_type="pdf", blocks=blocks, pages=pages, unprocessed_images=unprocessed, warnings=warnings
    )


def _pdf_page_blocks(page: Any, number: int) -> list[Block]:
    tables = page.find_tables()
    blocks: list[Block] = []
    table_boxes = [t.bbox for t in tables]
    for t in tables:
        rows = [[(c or "").strip() for c in r] for r in t.extract()]
        rows = [r for r in rows if any(r)]
        if rows:
            blocks.append(
                Block(
                    kind="table",
                    text=_table_text(rows[0], rows[1:]),
                    table_header=rows[0],
                    table_rows=rows[1:],
                    page=number,
                )
            )

    def outside_tables(obj: dict[str, float]) -> bool:
        return not any(b[0] <= obj["x0"] <= b[2] and b[1] <= obj["top"] <= b[3] for b in table_boxes)

    text_page = page.filter(outside_tables) if table_boxes else page
    lines = text_page.extract_text_lines(return_chars=True) or []
    sizes = [ch["size"] for ln in lines for ch in ln.get("chars", []) if ch.get("size")]
    body_size = statistics.median(sizes) if sizes else 10.0
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Block(kind="paragraph", text=" ".join(para), page=number))
            para.clear()

    prev_bottom = None
    for ln in lines:
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        size = statistics.median([c["size"] for c in ln.get("chars", []) if c.get("size")] or [body_size])
        is_heading = size >= body_size * 1.2 and len(text) < 120 and not text.endswith(".")
        gap = (ln["top"] - prev_bottom) if prev_bottom is not None else 0
        prev_bottom = ln["bottom"]
        if is_heading:
            flush()
            level = 1 if size >= body_size * 1.6 else 2
            blocks.append(Block(kind="heading", text=text, level=level, page=number))
            continue
        if gap > body_size * 0.9:
            flush()
        para.append(text)
    flush()
    # Order: tables were appended first; keep reading order by moving tables after preceding text blocks.
    return [b for b in blocks if b.kind != "table"] + [b for b in blocks if b.kind == "table"]


async def parse_document(data: bytes, media_type: MediaType, *, ocr: OcrFn | None = None) -> ParsedDocument:
    if media_type == "pdf":
        return await parse_pdf(data, ocr=ocr)
    if media_type == "docx":
        return parse_docx(data)
    if media_type == "html":
        return parse_html(data)
    return parse_text(data)
