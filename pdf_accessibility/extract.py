from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class PagePacket:
    page_number: int
    width: float
    height: float
    image_data_url: str
    embedded_text: str
    native_words: tuple[tuple[float, float, float, float, str], ...] = ()
    ocr_text: str = ""
    ocr_words: tuple[tuple[float, float, float, float, str], ...] = ()


def _compact_embedded_text(page: pymupdf.Page, limit: int = 24_000) -> str:
    blocks = page.get_text("blocks", sort=True)
    lines: list[str] = []
    for index, block in enumerate(blocks):
        if len(block) < 7 or block[6] != 0:
            continue
        x0, y0, x1, y1, text = block[:5]
        clean = " ".join(str(text).split())
        if clean:
            lines.append(f"block_{index} bbox=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}): {clean}")
    result = "\n".join(lines)
    return result[:limit]


def _words(page: pymupdf.Page) -> tuple[tuple[float, float, float, float, str], ...]:
    return tuple(
        (float(x0), float(y0), float(x1), float(y1), str(word))
        for x0, y0, x1, y1, word, *_ in page.get_text("words", sort=False)
    )


def extract_page_packets(
    pdf_path: Path,
    dpi: int = 150,
    evidence_pdf: Path | None = None,
) -> list[PagePacket]:
    packets: list[PagePacket] = []
    evidence_document = (
        pymupdf.open(evidence_pdf)
        if evidence_pdf and evidence_pdf.resolve() != pdf_path.resolve()
        else None
    )
    with pymupdf.open(pdf_path) as document:
        zoom = dpi / 72
        matrix = pymupdf.Matrix(zoom, zoom)
        for page_index, page in enumerate(document):
            evidence_page = (
                evidence_document[page_index]
                if evidence_document and page_index < evidence_document.page_count
                else page
            )
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            png = pixmap.tobytes("png")
            encoded = base64.b64encode(png).decode("ascii")
            packets.append(
                PagePacket(
                    page_number=page_index + 1,
                    width=page.rect.width,
                    height=page.rect.height,
                    image_data_url=f"data:image/png;base64,{encoded}",
                    embedded_text=_compact_embedded_text(page),
                    native_words=_words(page),
                    ocr_text=_compact_embedded_text(evidence_page),
                    ocr_words=_words(evidence_page),
                )
            )
    if evidence_document:
        evidence_document.close()
    return packets
