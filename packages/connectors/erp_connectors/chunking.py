"""Structure-aware chunking.

* Chunks never cross a heading boundary; each carries its heading path (section) and page range.
* Tables become their own chunks; large tables are split by rows and every fragment repeats the header.
* Chunks are linked (prev/next) within a section so neighbor expansion can recover context.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from erp_connectors.parsing import Block, ParsedDocument, _table_text

TARGET_CHARS = 1400  # ≈ 350 tokens
MAX_CHARS = 2000


@dataclass
class DraftChunk:
    content: str
    section: str
    section_id: str
    page: int | None
    page_end: int | None
    line: int | None
    chunk_type: str = "text"
    table_header: str | None = None
    ordinal: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def location(self) -> str:
        if self.page is not None:
            if self.page_end and self.page_end != self.page:
                return f"pp. {self.page}-{self.page_end}"
            return f"p. {self.page}"
        if self.line is not None:
            return f"line {self.line}"
        return self.section or "document"


def _section_id(path: list[str]) -> str:
    return "sec_" + hashlib.sha256(" > ".join(path).encode()).hexdigest()[:16]


def chunk_document(parsed: ParsedDocument, *, title: str) -> list[DraftChunk]:
    chunks: list[DraftChunk] = []
    path: list[tuple[int, str]] = []
    buf: list[Block] = []

    def section_path() -> list[str]:
        return [t for _, t in path] or [title]

    def flush() -> None:
        if not buf:
            return
        text = "\n".join(b.text for b in buf)
        pages = [b.page for b in buf if b.page is not None]
        sp = section_path()
        chunks.append(
            DraftChunk(
                content=text,
                section=" > ".join(sp),
                section_id=_section_id(sp),
                page=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                line=next((b.line for b in buf if b.line is not None), None),
            )
        )
        buf.clear()

    for block in parsed.blocks:
        if block.kind == "heading":
            flush()
            while path and path[-1][0] >= block.level:
                path.pop()
            path.append((block.level, block.text[:200]))
            continue
        if block.kind == "table":
            flush()
            sp = section_path()
            header = block.table_header or []
            rows = block.table_rows or []
            header_text = " | ".join(header)
            group: list[list[str]] = []
            for row in rows:
                if group and len(_table_text(header, [*group, row])) > TARGET_CHARS:
                    chunks.append(_table_chunk(header, group, header_text, sp, block))
                    group = []
                group.append(row)
            if group or not rows:
                chunks.append(_table_chunk(header, group, header_text, sp, block))
            continue
        # Long paragraph on its own: split on sentence-ish boundaries.
        if len(block.text) > MAX_CHARS:
            flush()
            for piece in _split_long(block.text):
                buf.append(block.model_copy(update={"text": piece}))
                flush()
            continue
        if sum(len(b.text) for b in buf) + len(block.text) > TARGET_CHARS:
            flush()
        buf.append(block)
    flush()

    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks


def _table_chunk(
    header: list[str], rows: list[list[str]], header_text: str, sp: list[str], block: Block
) -> DraftChunk:
    return DraftChunk(
        content=_table_text(header, rows),
        section=" > ".join(sp),
        section_id=_section_id([*sp, "table", header_text]),
        page=block.page,
        page_end=block.page,
        line=block.line,
        chunk_type="table",
        table_header=header_text,
    )


def _split_long(text: str) -> list[str]:
    pieces: list[str] = []
    current = ""
    for sentence in text.replace("\n", " ").split(". "):
        part = sentence if sentence.endswith(".") else sentence + "."
        if len(current) + len(part) > TARGET_CHARS and current:
            pieces.append(current.strip())
            current = ""
        current += part + " "
    if current.strip():
        pieces.append(current.strip())
    # Hard split anything still too long (e.g. no punctuation).
    out: list[str] = []
    for p in pieces:
        out.extend(p[i : i + MAX_CHARS] for i in range(0, len(p), MAX_CHARS))
    return out
