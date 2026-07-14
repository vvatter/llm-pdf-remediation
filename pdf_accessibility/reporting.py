from __future__ import annotations

import base64
from datetime import datetime, timezone
import difflib
import hashlib
import html
import json
from pathlib import Path
import subprocess

from .compiler import _find_anchor_font
from .extract import PagePacket
from .models import DocumentPlan, FindingCategory, RemediationMode
from .planner import PLANNER_PROMPT_VERSION, REVIEW_PROMPT_VERSION
from .plans import plan_sha256, sha256_file
from .preflight import PreflightReport


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0] if output else None


def _git_commit() -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def collect_anomalies(plan: DocumentPlan, threshold: float = 0.8) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for page in plan.pages:
        for message in page.page_ambiguities:
            records.append({"page": page.page_number, "severity": "warning", "category": FindingCategory.PAGE.value, "message": message})
        for finding in page.findings:
            records.append({"page": page.page_number, **finding.model_dump(mode="json")})
        for element in page.elements:
            for finding in element.findings:
                records.append(
                    {
                        "page": page.page_number,
                        "element_id": element.id,
                        "role": element.role.value,
                        **finding.model_dump(mode="json"),
                    }
                )
            if element.minimum_confidence < threshold:
                records.append(
                    {
                        "page": page.page_number,
                        "element_id": element.id,
                        "role": element.role.value,
                        "severity": "warning",
                        "category": FindingCategory.LOW_CONFIDENCE.value,
                        "message": "One or more confidence dimensions are below threshold.",
                        "confidence": element.confidence.model_dump(),
                        "chosen": element.semantic_text[:300],
                    }
                )
        agreements = [
            element.evidence.planner_reviewer_agreement
            for element in page.elements
            if element.evidence.planner_reviewer_agreement is not None
        ]
        if agreements and min(agreements) < 0.9:
            records.append(
                {
                    "page": page.page_number,
                    "severity": "info",
                    "category": FindingCategory.MODEL_DISAGREEMENT.value,
                    "message": "The proposal and canonical review differ materially on this page.",
                    "agreement": min(agreements),
                }
            )
    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for record in records:
        key = (
            record.get("page"),
            record.get("element_id"),
            record.get("severity"),
            record.get("category"),
            " ".join(str(record.get("message", "")).split()),
        )
        if key not in seen:
            seen.add(key)
            deduplicated.append(record)
    return deduplicated


def write_anomaly_reports(
    plan: DocumentPlan,
    packets: list[PagePacket],
    jsonl_path: Path,
    html_path: Path,
    threshold: float = 0.8,
    page_artifact_dir: Path | None = None,
) -> list[dict[str, object]]:
    records = collect_anomalies(plan, threshold)
    jsonl_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    by_page: dict[int, list[dict[str, object]]] = {}
    for record in records:
        by_page.setdefault(int(record["page"]), []).append(record)
    packet_by_page = {packet.page_number: packet for packet in packets}
    plan_by_page = {page.page_number: page for page in plan.pages}
    sections: list[str] = []
    for page_number in sorted(by_page):
        packet = packet_by_page.get(page_number)
        page_plan = plan_by_page.get(page_number)
        image = (
            f'<img src="{html.escape(packet.image_data_url)}" alt="Rendered page {page_number}">'
            if packet
            else ""
        )
        priority_entries = "".join(
            f"<li><strong>{html.escape(str(item.get('severity', 'info')))}: "
            f"{html.escape(str(item.get('category', 'finding')))}</strong> "
            f"{html.escape(str(item.get('message', '')))}"
            f"<pre>{html.escape(json.dumps(item, ensure_ascii=False, indent=2))}</pre></li>"
            for item in by_page[page_number]
            if item.get("severity") != "info"
        )
        info_entries = "".join(
            f"<li><strong>{html.escape(str(item.get('category', 'finding')))}</strong> "
            f"{html.escape(str(item.get('message', '')))}</li>"
            for item in by_page[page_number]
            if item.get("severity") == "info"
        )
        canonical = "\n\n".join(
            f"[{element.role.value}] {element.semantic_text}"
            for element in (page_plan.elements if page_plan else [])
        )
        proposal_text = ""
        if page_artifact_dir:
            proposal_path = page_artifact_dir / f"{page_number:04d}.proposal.json"
            if proposal_path.exists():
                proposal_data = json.loads(proposal_path.read_text(encoding="utf-8"))
                proposal_page = proposal_data.get("proposal", proposal_data)
                proposal_text = "\n\n".join(
                    f"[{element.get('role', '')}] "
                    + str(
                        element.get("alt_text")
                        if element.get("role") == "Figure"
                        else element.get("accessible_text", element.get("text", ""))
                    )
                    for element in proposal_page.get("elements", [])
                )
        diff = "\n".join(
            difflib.unified_diff(
                proposal_text.splitlines(),
                canonical.splitlines(),
                fromfile="proposal",
                tofile="canonical-review",
                lineterm="",
            )
        )
        evidence_text = (
            f"NATIVE EVIDENCE\n{packet.embedded_text}\n\nOCR EVIDENCE\n{packet.ocr_text}"
            if packet
            else ""
        )
        details = (
            (f"<details><summary>Informational findings ({sum(item.get('severity') == 'info' for item in by_page[page_number])})</summary><ul>{info_entries}</ul></details>" if info_entries else "")
            +
            f"<details open><summary>Canonical transcript</summary><pre>{html.escape(canonical)}</pre></details>"
            f"<details><summary>Proposal/review diff</summary><pre>{html.escape(diff or '(no difference)')}</pre></details>"
            f"<details><summary>Native and OCR evidence</summary><pre>{html.escape(evidence_text)}</pre></details>"
        )
        sections.append(
            f"<section><h2>Page {page_number}</h2><div class=\"columns\"><div>{image}</div>"
            f"<div><ol>{priority_entries}</ol>{details}</div></div></section>"
        )
    html_path.write_text(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Remediation anomalies</title>"
        "<style>body{font:16px system-ui;margin:2rem;max-width:1100px}section{border-top:1px solid #aaa;padding:1rem 0}"
        "img{max-width:100%;height:auto}.columns{display:grid;grid-template-columns:minmax(280px,1fr) minmax(320px,1fr);gap:2rem}"
        "pre{white-space:pre-wrap;background:#f4f4f4;padding:.75rem;font-size:12px;max-height:32rem;overflow:auto}"
        "@media(max-width:800px){.columns{grid-template-columns:1fr}}</style>"
        f"<h1>Remediation anomalies</h1><p>{len(records)} recorded findings. This report is read-only.</p>"
        + "".join(sections)
        + "</html>\n",
        encoding="utf-8",
    )
    return records


def wcag_evidence(
    plan: DocumentPlan,
    mode: RemediationMode,
    validation: dict[str, object],
) -> dict[str, object]:
    rows = [
        ("1.1.1", "Non-text Content", "review", "Figure alternate text exists; quality requires human or AT review."),
        ("1.3.1", "Info and Relationships", "pass" if validation.get("structure_matches_plan") else "fail", "Structure tree matches the approved semantic plan."),
        ("1.3.2", "Meaningful Sequence", "pass" if validation.get("structure_matches_plan") else "fail", "Structure-tree transcript matches canonical visual page order."),
        ("1.4.3", "Contrast (Minimum)", "not_tested", "Requires visual or measurement review of the historical page."),
        (
            "1.4.5",
            "Images of Text",
            "review",
            "Facsimile mode needs an essential-presentation rationale; native mode still needs source inspection.",
        ),
        ("1.4.10", "Reflow", "not_tested", "Requires Acrobat reflow and narrow-window testing."),
        ("2.4.2", "Page Titled", "pass" if validation.get("title") else "fail", "PDF title metadata is present."),
        ("2.4.6", "Headings and Labels", "review", "Heading tags exist; wording and hierarchy require semantic review."),
        ("3.1.1", "Language of Page", "pass" if validation.get("language") else "fail", "Document language metadata is present."),
    ]
    return {
        "standard": "WCAG 2.1 AA",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_review_status": plan.review_status.value,
        "criteria": [
            {"criterion": criterion, "name": name, "result": result, "evidence": evidence, "reviewer": None}
            for criterion, name, result, evidence in rows
        ],
    }


def build_manifest(
    source: Path,
    plan: DocumentPlan,
    preflight: PreflightReport,
    packets: list[PagePacket],
    planner_model: str,
    reviewer_model: str,
    planner_reasoning: str,
    reviewer_reasoning: str,
    validation: dict[str, object],
    page_artifact_dir: Path | None = None,
    geometry_sources: list[str] | None = None,
) -> dict[str, object]:
    font_path = _find_anchor_font()
    page_hashes = [
        hashlib.sha256(base64.b64decode(packet.image_data_url.split(",", 1)[1])).hexdigest()
        for packet in packets
    ]
    responses: list[dict[str, object]] = []
    if page_artifact_dir and page_artifact_dir.exists():
        for artifact in sorted(page_artifact_dir.glob("*.proposal.json")) + sorted(
            page_artifact_dir.glob("*.review.json")
        ):
            data = json.loads(artifact.read_text(encoding="utf-8"))
            responses.append(
                {
                    "artifact": artifact.name,
                    "model": data.get("model"),
                    "response_id": data.get("response_id"),
                    "prompt_version": data.get("prompt_version"),
                    "reasoning_effort": data.get("reasoning_effort"),
                }
            )
    return {
        "manifest_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "source_sha256": sha256_file(source),
        "page_image_sha256": page_hashes,
        "plan_sha256": plan_sha256(plan),
        "plan_schema_version": plan.schema_version,
        "mode": preflight.selected_mode.value,
        "models": {
            "proposal": {"id": planner_model, "reasoning_effort": planner_reasoning, "prompt_version": PLANNER_PROMPT_VERSION},
            "review": {"id": reviewer_model, "reasoning_effort": reviewer_reasoning, "prompt_version": REVIEW_PROMPT_VERSION},
        },
        "responses": responses,
        "ocr": {
            "force_ocr": preflight.selected_mode == RemediationMode.FACSIMILE,
            "output_type": "pdf",
            "optimize": 3,
            "jpeg_quality": 88,
            "png_quality": 90,
            "oversample_dpi": 300,
        },
        "compiler_strategy": {
            "marked_content_granularity": "visual_region",
            "unicode_text_strategy": "direct_unicode_per_ocr_line",
            "actual_text_strategy": "exceptional_region_fallback_only",
            "geometry_alignment": "block_local_with_ocr_line_identity",
            "structure_regions": "direct_visual_regions_integer_mcids",
            "fragmented_paragraph_strategy": "separate_direct_paragraph_regions",
            "text_object_scope": "visual_region",
            "content_stream_scope": "visual_region",
            "figure_proxy": "nonpainting_geometry_with_structural_alt",
            "layout_attributes": "word_union_bbox",
            "parent_tree_granularity": "visual_region",
            "target_reader_profile": "acrobat",
        },
        "geometry_sources": geometry_sources or [],
        "font": {"path": str(font_path), "sha256": sha256_file(font_path)},
        "tools": {
            "git_commit": _git_commit(),
            "qpdf": _version(["qpdf", "--version"]),
            "ocrmypdf": _version(["ocrmypdf", "--version"]),
            "tesseract": _version(["tesseract", "--version"]),
            "verapdf": _version(["verapdf", "--version"]),
        },
        "validation": {
            "released": validation.get("released", False),
            "visual_match": validation.get("visual_match"),
            "source_visual_fidelity_ok": validation.get("source_visual_fidelity_ok"),
            "structure_matches_plan": validation.get("structure_matches_plan"),
            "extraction_compatible": validation.get("extraction_compatible"),
            "transformations_valid": validation.get("transformations_valid"),
            "block_plan_valid": validation.get("block_plan_valid"),
            "verapdf_pdfua_ok": validation.get("verapdf_pdfua_ok"),
        },
    }
