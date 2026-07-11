from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pikepdf
import pymupdf


def _render_hashes(path: Path, dpi: int = 120) -> list[str]:
    hashes: list[str] = []
    with pymupdf.open(path) as document:
        matrix = pymupdf.Matrix(dpi / 72, dpi / 72)
        for page in document:
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            hashes.append(hashlib.sha256(pixmap.samples).hexdigest())
    return hashes


def validate_output(source: Path, output: Path) -> dict[str, object]:
    before = _render_hashes(source)
    after = _render_hashes(output)
    qpdf = subprocess.run(
        ["qpdf", "--check", str(output)], capture_output=True, text=True, check=False
    )
    with pikepdf.Pdf.open(output) as pdf:
        has_structure_tree = "/StructTreeRoot" in pdf.Root
        marked = bool(pdf.Root.get("/MarkInfo", {}).get("/Marked", False))
        tagged_pages = sum("/StructParents" in page.obj for page in pdf.pages)
        language = str(pdf.Root.get("/Lang", ""))
        title = str(pdf.docinfo.get("/Title", ""))
        structure_elements = 0
        accessible_payloads = 0
        if has_structure_tree:
            document = pdf.Root.StructTreeRoot.K
            structure_elements = len(document.K)
            for element in document.K:
                if str(element.S) == "/Figure":
                    accessible_payloads += bool(str(element.get("/Alt", "")).strip())
                else:
                    children = element.get("/K", [])
                    if isinstance(children, pikepdf.Dictionary):
                        children = [children]
                    accessible_payloads += bool(children) and all(
                        isinstance(content_item, pikepdf.Dictionary)
                        and "/MCID" in content_item
                        for content_item in children
                    )
        with pdf.open_outline() as outline:
            bookmark_count = len(outline.root)
    return {
        "source": str(source),
        "output": str(output),
        "visual_match": before == after,
        "page_count_match": len(before) == len(after),
        "qpdf_ok": qpdf.returncode == 0,
        "qpdf_output": (qpdf.stdout + qpdf.stderr).strip(),
        "has_structure_tree": has_structure_tree,
        "marked": marked,
        "tagged_pages": tagged_pages,
        "fully_tagged": tagged_pages == len(pdf.pages),
        "language": language,
        "title": title,
        "structure_elements": structure_elements,
        "accessible_payloads": accessible_payloads,
        "all_elements_have_accessible_text": accessible_payloads == structure_elements,
        "bookmark_count": bookmark_count,
    }


def write_validation_report(source: Path, output: Path, report_path: Path) -> dict[str, object]:
    report = validate_output(source, output)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
