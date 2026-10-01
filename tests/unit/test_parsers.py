import pytest

from erp_connectors.chunking import chunk_document
from erp_connectors.parsing import (
    ProtectedContentError,
    ValidationFailure,
    parse_docx,
    parse_html,
    parse_pdf,
    parse_text,
    validate_file,
)
from erp_evals import fixtures


async def test_pdf_layout_headings_tables_pages() -> None:
    parsed = await parse_pdf(fixtures.pdf_with_table())
    assert parsed.page_count == 2 and parsed.coverage_ratio == 1.0
    headings = [b.text for b in parsed.blocks if b.kind == "heading"]
    assert "Shipping Overview" in headings and "Regional Shipping Fees" in headings
    tables = [b for b in parsed.blocks if b.kind == "table"]
    assert tables and tables[0].table_header == ["Region", "Shipping fee (USD)", "Delivery days"]
    assert tables[0].page == 2
    chunks = chunk_document(parsed, title="Logistics Handbook")
    table_chunks = [c for c in chunks if c.chunk_type == "table"]
    assert table_chunks[0].table_header == "Region | Shipping fee (USD) | Delivery days"
    assert table_chunks[0].location == "p. 2"
    assert "Regional Shipping Fees" in table_chunks[0].section


async def test_scanned_page_is_reported_not_dropped() -> None:
    parsed = await parse_pdf(fixtures.scanned_pdf())
    assert parsed.page_count == 2 and parsed.covered_pages == 1
    report = parsed.coverage_report()
    assert report["uncovered"][0]["page"] == 2
    assert report["uncovered"][0]["status"] == "no_text"
    assert "OCR not configured" in report["uncovered"][0]["detail"]


async def test_scanned_page_uses_ocr_when_configured() -> None:
    calls: list[int] = []

    async def fake_ocr(_pdf: bytes, page: int) -> str:  # test double for the Textract adapter
        calls.append(page)
        return "Scanned page: warranty period is 24 months."

    parsed = await parse_pdf(fixtures.scanned_pdf(), ocr=fake_ocr)
    assert calls == [2] and parsed.coverage_ratio == 1.0
    assert any(p.status == "ocr" for p in parsed.pages)


async def test_encrypted_pdf_is_protected_content() -> None:
    with pytest.raises(ProtectedContentError):
        await parse_pdf(fixtures.encrypted_pdf())


def test_docx_headings_and_table() -> None:
    parsed = parse_docx(fixtures.docx_with_table())
    kinds = [(b.kind, b.text) for b in parsed.blocks]
    assert ("heading", "Password Rules") in kinds
    table = next(b for b in parsed.blocks if b.kind == "table")
    assert table.table_header == ["Control", "Frequency"]


def test_html_strips_scripts_and_reports_images() -> None:
    html = (
        b"<html><body><h1>T</h1><script>steal()</script><p>Visible paragraph text here.</p>"
        b"<img src='x.png' alt='Org chart'><img src='y.png'></body></html>"
    )
    parsed = parse_html(html)
    text = " ".join(b.text for b in parsed.blocks)
    assert "steal" not in text and "[Image: Org chart]" in text
    assert parsed.unprocessed_images == 2


def test_markdown_tables_and_headings() -> None:
    parsed = parse_text(b"# Title\n\nIntro text paragraph.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert [b.kind for b in parsed.blocks] == ["heading", "paragraph", "table"]


@pytest.mark.parametrize(
    ("data", "name", "code"),
    [
        (b"", "a.txt", "empty_file"),
        (b"%PDF-1.7 fake", "a.txt", "type_mismatch"),
        (b"plain text", "a.pdf", "type_mismatch"),
        (b"\x00\x01\x02\xff\xfe binary", "a.txt", "unsupported_binary"),
        (b"PK\x03\x04garbage", "a.docx", "corrupt_archive"),
        (b"x" * 101, "a.txt", "file_too_large"),
    ],
)
def test_validation_failures(data: bytes, name: str, code: str) -> None:
    with pytest.raises(ValidationFailure) as exc:
        validate_file(data, name, max_bytes=100)
    assert exc.value.code.startswith(code)


def test_chunks_do_not_cross_headings_and_split_long_tables() -> None:
    rows = "\n".join(f"| item {i} | {'x' * 60} |" for i in range(80))
    parsed = parse_text(f"# A\n\npara a\n\n# B\n\npara b\n\n| K | V |\n|---|---|\n{rows}\n".encode())
    chunks = chunk_document(parsed, title="T")
    assert [c.section for c in chunks[:2]] == ["A", "B"]
    tables = [c for c in chunks if c.chunk_type == "table"]
    assert len(tables) > 1 and all(c.content.startswith("K | V") for c in tables)
