"""Amazon Textract OCR for scanned (image-only) PDF pages.

Uses synchronous ``DetectDocumentText`` on a single-page PDF extracted with pypdf. Lines are grouped into
paragraphs by vertical gaps. Live status: implemented-not-live-tested.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import boto3
from pypdf import PdfReader, PdfWriter


class TextractOcr:
    def __init__(self, *, region: str, client: Any = None) -> None:
        self._client = client or boto3.client("textract", region_name=region)

    async def ocr_page(self, pdf_bytes: bytes, page_number: int) -> str:
        def single_page() -> bytes:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            writer = PdfWriter()
            writer.add_page(reader.pages[page_number - 1])
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()

        page_pdf = await asyncio.to_thread(single_page)
        resp = await asyncio.to_thread(self._client.detect_document_text, Document={"Bytes": page_pdf})
        lines = [b for b in resp.get("Blocks", []) if b.get("BlockType") == "LINE"]
        paragraphs: list[list[str]] = []
        prev_bottom: float | None = None
        for line in lines:
            box = line.get("Geometry", {}).get("BoundingBox", {})
            top, height = float(box.get("Top", 0)), float(box.get("Height", 0))
            if prev_bottom is None or top - prev_bottom > height * 0.8:
                paragraphs.append([])
            paragraphs[-1].append(str(line.get("Text", "")))
            prev_bottom = top + height
        return "\n\n".join(" ".join(p) for p in paragraphs)
