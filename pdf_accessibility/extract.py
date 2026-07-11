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


def extract_page_packets(pdf_path: Path, dpi: int = 150) -> list[PagePacket]:
    packets: list[PagePacket] = []
    with pymupdf.open(pdf_path) as document:
        zoom = dpi / 72
        matrix = pymupdf.Matrix(zoom, zoom)
        for page_index, page in enumerate(document):
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
                )
            )
    return packets
