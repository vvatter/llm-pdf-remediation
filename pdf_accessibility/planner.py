from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import shutil
from typing import Literal

from openai import OpenAI
import pymupdf
from pydantic import BaseModel, Field, ValidationError

from .evidence import (
    PageEvidence,
    PlanningDiagnostics,
    diagnostics_for,
    evidence_from_packet,
    text_agreement,
)
from .extract import PagePacket
from .models import (
    DocumentPlan,
    ArtifactReason,
    ArtifactRecord,
    ElementRole,
    FindingCategory,
    PagePlan,
    PageFlow,
    PageReview,
    ReviewFinding,
    ReviewSeverity,
    ReviewStatus,
    ConfidenceProfile,
    TextFragment,
    TextTransformation,
    TransformationKind,
    exact_text_tokens,
)
from .plans import load_document_plan, sha256_file, write_document_plan


PLANNER_PROMPT_VERSION = "proposal-v5"
REVIEW_PROMPT_VERSION = "review-v5"

PROPOSAL_SYSTEM_PROMPT = """You propose an accessibility transcription and semantic plan for a
historical fixed-layout PDF page. The printed page is the historical source of record. Preserve
its spelling, punctuation, capitalization, names, numbers, formula notation, and authored errors.
Never silently regularize or correct it.

For every meaningful item, record visible_text exactly as printed and accessible_text as spoken.
They should be identical except for a declared mechanical transformation: line-break
dehyphenation, ligature expansion, soft-hyphen removal, formula spoken equivalent, decorative
leader/marker omission, structural separator normalization, date-range spoken expansion, or
whitespace normalization. Record every such transformation. Native and OCR
text are evidence only and can be corrupt. Always choose a result, preserve visual page order,
and record uncertainty in findings rather than stopping.

Use DocumentTitle once on the first page; H1 for article titles; H2/H3 for genuine nested
headings; P for body copy, bylines, quotations, contents entries, captions, and other meaningful
text; Figure for meaningful graphics with concise alt text. Omit repeated headers, footers,
page numbers that add no meaning, and decoration. Join line-broken body copy into paragraphs.
Every bbox must use normalized_0_1000 coordinates: left and right are percentages of page width
times 1000; top and bottom are percentages of page height times 1000. Never return PDF points or
image pixels. Give separate transcription, semantic-role, geometry, and reading-order confidence
values.

Decompose every logical element into atomic rectangular visible_fragments. A fragment may not span
disjoint columns. Give each fragment a unique block ID such as b001. When one paragraph continues
from the bottom of one column to the top of another, keep one logical P element with two ordered
fragments. Return flows whose block_ids contain every meaningful fragment exactly once in logical
reading order. Finish an article flow before an independent sidebar or contents flow even when
strict global top-to-bottom coordinates would interleave them. On a first-page masthead, order the
publication title, descriptive publication line, volume/issue/date metadata, and then any epigraph
or attributed quotation before the first article heading."""

REVIEW_SYSTEM_PROMPT = """You are the independent final semantic reviewer for a historical PDF
accessibility plan. The page image and printed content are authoritative. Evidence text may be
corrupt. Inspect the image and evidence before considering the first model's proposal. Return one
canonical PagePlan, choosing explicitly among plausible readings. Do not modernize, summarize,
correct spelling, expand abbreviations, or invent obscured words. Preserve visible wording and
declare every allowed accessibility-only transformation. Preserve visual page order. Findings,
including critical findings, are advisories: always choose a canonical result and continue.
Use separate confidence dimensions and log alternatives for names, dates, numbers, URLs,
formulas, uncertain transcription, roles, geometry, and reading order. Return atomic rectangular
visible fragments with unique block IDs plus flows that own every block exactly once. Preserve a
single logical paragraph across multiple ordered fragments when it continues between columns. On
a first-page masthead, keep any epigraph or attributed quotation after publication and issue
metadata and before the first article heading."""


class ModelPageElement(BaseModel):
    role: ElementRole
    visible_fragments: list[TextFragment] = Field(default_factory=list)
    visible_text: str
    accessible_text: str
    transformations: list[TextTransformation] = Field(default_factory=list)
    alt_text: str | None = None
    bbox: list[float] = Field(min_length=4, max_length=4)
    confidence: ConfidenceProfile
    findings: list[ReviewFinding] = Field(default_factory=list)


class ModelPagePlan(BaseModel):
    page_number: int = Field(ge=1)
    coordinate_space: Literal["normalized_0_1000"] = "normalized_0_1000"
    elements: list[ModelPageElement]
    flows: list[PageFlow] = Field(default_factory=list)
    page_ambiguities: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)


class ModelReviewDecision(BaseModel):
    canonical_page: ModelPagePlan
    findings: list[ReviewFinding] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    canonical_page: PagePlan
    findings: list[ReviewFinding] = Field(default_factory=list)


def _validated_page_plan(page_data: dict, page_number: int) -> PagePlan:
    page_data["page_number"] = page_number
    try:
        return PagePlan.model_validate(page_data)
    except ValidationError as error:
        if not error.errors() or any(
            "flow" not in str(item.get("msg", "")).lower()
            for item in error.errors()
        ):
            raise
    normalized = dict(page_data)
    normalized["flows"] = []
    page = PagePlan.model_validate(normalized)
    page.findings.append(
        ReviewFinding(
            severity=ReviewSeverity.INFO,
            category=FindingCategory.READING_ORDER,
            message=(
                "Model flow grouping was inconsistent with semantic element order; "
                "the deterministic parser rebuilt a single ordered page flow."
            ),
            chosen="Semantic element and fragment order.",
        )
    )
    return page


def _page_prompt(packet: PagePacket, evidence: PageEvidence) -> str:
    return f"""Analyze page {packet.page_number}, size {packet.width:.1f} by {packet.height:.1f} points.

NATIVE EVIDENCE (advisory):
{evidence.native_text or '(none)'}

OCR EVIDENCE (advisory):
{evidence.ocr_text or '(none)'}
"""


def propose_page(
    client: OpenAI,
    model: str,
    reasoning_effort: str,
    packet: PagePacket,
    evidence: PageEvidence,
) -> tuple[PagePlan, str | None]:
    response = client.responses.parse(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=[
            {"role": "system", "content": PROPOSAL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": packet.image_data_url, "detail": "high"},
                    {"type": "input_text", "text": _page_prompt(packet, evidence)},
                ],
            },
        ],
        text_format=ModelPagePlan,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"{model} returned no parsed proposal for page {packet.page_number}")
    page_data = response.output_parsed.model_dump()
    page = _validated_page_plan(page_data, packet.page_number)
    page.review_status = ReviewStatus.PROPOSAL
    for element in page.elements:
        element.review_status = ReviewStatus.PROPOSAL
    return page, response.id


def review_page(
    client: OpenAI,
    model: str,
    reasoning_effort: str,
    packet: PagePacket,
    evidence: PageEvidence,
    diagnostics: PlanningDiagnostics,
    proposal: PagePlan,
) -> tuple[ReviewDecision, str | None]:
    response = client.responses.parse(
        model=model,
        reasoning={"effort": reasoning_effort},
        input=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": packet.image_data_url, "detail": "high"},
                    {
                        "type": "input_text",
                        "text": (
                            "EVIDENCE FIRST:\n"
                            f"Page size: {evidence.width:.1f} by {evidence.height:.1f} points\n"
                            "Canonical bbox coordinate space: normalized_0_1000 only\n"
                            f"Native word count: {len(evidence.native_words)}\n"
                            f"OCR word count: {len(evidence.ocr_words)}\n"
                            f"NATIVE TEXT:\n{evidence.native_text or '(none)'}\n\n"
                            f"OCR TEXT:\n{evidence.ocr_text or '(none)'}"
                        ),
                    },
                    {
                        "type": "input_text",
                        "text": "DETERMINISTIC DIAGNOSTICS:\n" + diagnostics.model_dump_json(indent=2),
                    },
                    {
                        "type": "input_text",
                        "text": "FIRST-MODEL PROPOSAL:\n"
                        + json.dumps(
                            proposal.model_dump(
                                mode="json",
                                exclude={
                                    "elements": {
                                        "__all__": {"tokens", "evidence", "review_status"}
                                    }
                                },
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ),
                    },
                ],
            },
        ],
        text_format=ModelReviewDecision,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"{model} returned no parsed review for page {packet.page_number}")
    parsed = response.output_parsed
    page_data = parsed.canonical_page.model_dump()
    return ReviewDecision(
        canonical_page=_validated_page_plan(page_data, packet.page_number),
        findings=parsed.findings,
    ), response.id


def infer_title(source: Path, pages: list[PagePlan]) -> str:
    for page in pages:
        for element in page.elements:
            if element.role == ElementRole.DOCUMENT_TITLE:
                return element.accessible_text.strip()
    return source.stem.replace("_", " ").strip().title()


def normalize_document_pages(source: Path, pages: list[PagePlan]) -> list[PagePlan]:
    canonical_title = infer_title(source, pages)
    kept_document_title = False
    normalized_title = " ".join(canonical_title.lower().split())

    for page in pages:
        cleaned = []
        for element in page.elements:
            if element.accessible_text.lower().startswith(("inside this issue", "contents ")):
                original = element.accessible_text
                normalized = re.sub(r"(?:\s*\.\s*){3,}", " ", original)
                normalized = " ".join(normalized.split())
                if normalized != original:
                    element.accessible_text = normalized
                    element.tokens = exact_text_tokens(normalized)
                    element.transformations.append(
                        TextTransformation(
                            kind=TransformationKind.DECORATIVE_LEADER_OMISSION,
                            source_text=original,
                            replacement_text=normalized,
                            rationale="Dot leaders are visual navigation and create noisy speech.",
                        )
                    )
            text = " ".join(element.accessible_text.lower().split())
            is_running_header = (
                page.page_number > 1
                and bool(normalized_title)
                and normalized_title in text
            )
            if element.role == ElementRole.DOCUMENT_TITLE:
                if page.page_number == 1 and not kept_document_title:
                    kept_document_title = True
                elif is_running_header:
                    page.artifacts.append(
                        ArtifactRecord(
                            reason=ArtifactReason.RUNNING_FURNITURE,
                            bbox=element.bbox,
                            text=element.visible_text or element.accessible_text,
                        )
                    )
                    continue
                else:
                    element.role = ElementRole.H1
            elif is_running_header and element.bbox[1] < 180:
                page.artifacts.append(
                    ArtifactRecord(
                        reason=ArtifactReason.RUNNING_FURNITURE,
                        bbox=element.bbox,
                        text=element.visible_text or element.accessible_text,
                    )
                )
                continue

            if (
                element.role == ElementRole.FIGURE
                and element.alt_text
                and any(word in element.alt_text.lower() for word in ("decorative", "flourish", "ornament"))
            ):
                page.artifacts.append(
                    ArtifactRecord(
                        reason=ArtifactReason.DECORATION,
                        bbox=element.bbox,
                        text=element.alt_text,
                    )
                )
                continue
            cleaned.append(element)
        page.elements = cleaned
        page.reconcile_flows()
    return pages


def _read_page_artifact(
    path: Path, key: str, source_hash: str
) -> tuple[PagePlan, str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("source_sha256") not in {None, source_hash}:
        raise RuntimeError(f"checkpoint {path} belongs to a different source PDF")
    if key in data:
        return PagePlan.model_validate(data[key]), data.get("response_id")
    return PagePlan.model_validate(data), None


def _write_once(path: Path, data: dict[str, object]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _archive(path: Path) -> None:
    if path.exists():
        destination = path.with_suffix(path.suffix + ".previous")
        shutil.copy2(path, destination)
        path.unlink()


def build_document_plan(
    source: Path,
    packets: list[PagePacket],
    checkpoint_path: Path,
    planner_model: str = "gpt-5.6-terra",
    reviewer_model: str = "gpt-5.6-sol",
    planner_reasoning: str = "medium",
    reviewer_reasoning: str = "high",
    workers: int = 2,
    force_replan: bool = False,
    force_review: bool = False,
) -> DocumentPlan:
    source_hash = sha256_file(source)
    with pymupdf.open(source) as source_document:
        source_page_count = source_document.page_count
    requested_pages = {packet.page_number for packet in packets}
    legacy_pages: dict[int, PagePlan] = {}
    if checkpoint_path.exists() and not force_replan:
        existing_plan = load_document_plan(checkpoint_path, source)
        if existing_plan.source_sha256 and existing_plan.source_sha256 != source_hash:
            raise RuntimeError("saved plan source hash does not match the input PDF")
        existing_pages = {page.page_number for page in existing_plan.pages}
        if (
            existing_plan.review_status
            in {ReviewStatus.MODEL_REVIEWED, ReviewStatus.MANUAL_MODIFIED}
            and requested_pages <= existing_pages
            and not force_review
        ):
            return existing_plan
        legacy_pages = {page.page_number: page for page in existing_plan.pages}
    elif force_replan:
        _archive(checkpoint_path)

    page_dir = checkpoint_path.parent / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    packet_by_page = {packet.page_number: packet for packet in packets}
    evidence_by_page = {number: evidence_from_packet(packet) for number, packet in packet_by_page.items()}
    proposals: dict[int, tuple[PagePlan, str | None]] = {}

    for number, evidence in evidence_by_page.items():
        evidence_path = page_dir / f"{number:04d}.evidence.json"
        if evidence_path.exists():
            saved_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if saved_evidence.get("source_sha256") not in {None, source_hash}:
                raise RuntimeError(
                    f"checkpoint {evidence_path} belongs to a different source PDF"
                )
        _write_once(
            evidence_path,
            {
                "schema_version": 4,
                "source_sha256": source_hash,
                "evidence": evidence.model_dump(mode="json"),
            },
        )
        proposal_path = page_dir / f"{number:04d}.proposal.json"
        if force_replan:
            _archive(proposal_path)
        if proposal_path.exists():
            proposals[number] = _read_page_artifact(
                proposal_path, "proposal", source_hash
            )
        elif number in legacy_pages:
            proposals[number] = (legacy_pages[number], None)
            _write_once(
                proposal_path,
                {
                    "schema_version": 4,
                    "source_sha256": source_hash,
                    "model": "legacy-plan-migration",
                    "response_id": None,
                    "prompt_version": "legacy-v1",
                    "proposal": legacy_pages[number].model_dump(mode="json"),
                },
            )

    pending = [number for number in sorted(packet_by_page) if number not in proposals]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                propose_page,
                client,
                planner_model,
                planner_reasoning,
                packet_by_page[number],
                evidence_by_page[number],
            ): number
            for number in pending
        }
        for future in as_completed(futures):
            number = futures[future]
            proposal, response_id = future.result()
            proposals[number] = (proposal, response_id)
            _write_once(
                page_dir / f"{number:04d}.proposal.json",
                {
                    "schema_version": 4,
                    "source_sha256": source_hash,
                    "model": planner_model,
                    "reasoning_effort": planner_reasoning,
                    "response_id": response_id,
                    "prompt_version": PLANNER_PROMPT_VERSION,
                    "proposal": proposal.model_dump(mode="json"),
                },
            )
            print(f"proposed page {number}/{len(packets)}")

    reviews: dict[int, PageReview] = {}
    for number in sorted(packet_by_page):
        review_path = page_dir / f"{number:04d}.review.json"
        if force_review or force_replan:
            _archive(review_path)
        if review_path.exists():
            data = json.loads(review_path.read_text(encoding="utf-8"))
            if data.get("source_sha256") not in {None, source_hash}:
                raise RuntimeError(f"checkpoint {review_path} belongs to a different source PDF")
            reviews[number] = PageReview.model_validate(data.get("review", data))

    pending_reviews = [number for number in sorted(packet_by_page) if number not in reviews]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {}
        for number in pending_reviews:
            proposal, _ = proposals[number]
            diagnostics = diagnostics_for(proposal, evidence_by_page[number])
            futures[
                executor.submit(
                    review_page,
                    client,
                    reviewer_model,
                    reviewer_reasoning,
                    packet_by_page[number],
                    evidence_by_page[number],
                    diagnostics,
                    proposal,
                )
            ] = (number, diagnostics)
        for future in as_completed(futures):
            number, diagnostics = futures[future]
            decision, response_id = future.result()
            canonical = decision.canonical_page
            canonical.page_number = number
            canonical.review_status = ReviewStatus.MODEL_REVIEWED
            proposal, proposal_response_id = proposals[number]
            planner_reviewer_agreement = text_agreement(
                "\n".join(element.semantic_text for element in proposal.elements),
                "\n".join(element.semantic_text for element in canonical.elements),
            )
            for element in canonical.elements:
                element.review_status = ReviewStatus.MODEL_REVIEWED
                element.evidence.native_agreement = diagnostics.proposal_native_agreement
                element.evidence.ocr_agreement = diagnostics.proposal_ocr_agreement
                element.evidence.planner_reviewer_agreement = planner_reviewer_agreement
            review = PageReview(
                page_number=number,
                canonical_page=canonical,
                findings=decision.findings,
                proposal_model=planner_model,
                reviewer_model=reviewer_model,
                proposal_response_id=proposal_response_id,
                reviewer_response_id=response_id,
            )
            reviews[number] = review
            _write_once(
                page_dir / f"{number:04d}.review.json",
                {
                    "schema_version": 4,
                    "source_sha256": source_hash,
                    "model": reviewer_model,
                    "reasoning_effort": reviewer_reasoning,
                    "response_id": response_id,
                    "prompt_version": REVIEW_PROMPT_VERSION,
                    "review": review.model_dump(mode="json"),
                },
            )
            print(f"reviewed page {number}/{len(packets)}")

    pages = normalize_document_pages(
        source, [reviews[number].canonical_page for number in sorted(reviews)]
    )
    plan = DocumentPlan(
        source_file=source.name,
        source_sha256=source_hash,
        source_page_count=source_page_count,
        title=infer_title(source, pages),
        language="en-US",
        pages=pages,
        review_status=ReviewStatus.MODEL_REVIEWED,
        plan_revision=2,
    )
    write_document_plan(plan, checkpoint_path)
    return plan
