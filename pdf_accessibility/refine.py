from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import re

import pymupdf

from .models import (
    ArtifactReason,
    ArtifactRecord,
    CoordinateSpace,
    DocumentPlan,
    ElementRole,
    FindingCategory,
    PageElement,
    ReviewFinding,
    ReviewSeverity,
    TextTransformation,
    TransformationKind,
    exact_text_tokens,
)


_FORMULA_CHARACTERS = set("=<>≤≥≠≈∞∑∫√∂∆∇^₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹ⁿ")
_LIGATURES = set("ﬀﬁﬂﬃﬄﬅﬆ")
_GENERIC_ALT_TEXT = {
    "historical document image",
    "historical image",
    "document image",
}


def _normalized_bbox(
    bbox: list[float], width: float, height: float, coordinate_space: CoordinateSpace
) -> list[float]:
    left, top, right, bottom = (float(value) for value in bbox)
    if coordinate_space == CoordinateSpace.PDF_POINTS:
        left, right = left / width * 1000, right / width * 1000
        top, bottom = top / height * 1000, bottom / height * 1000
    left, right = sorted((max(0.0, min(1000.0, left)), max(0.0, min(1000.0, right))))
    top, bottom = sorted((max(0.0, min(1000.0, top)), max(0.0, min(1000.0, bottom))))
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def normalize_plan_geometry(source: Path, plan: DocumentPlan) -> None:
    """Convert every plan box to the schema's explicit 0..1000 coordinate space."""
    with pymupdf.open(source) as document:
        for page in plan.pages:
            pdf_page = document[page.page_number - 1]
            width, height = float(pdf_page.rect.width), float(pdf_page.rect.height)
            coordinate_space = page.coordinate_space
            if coordinate_space == CoordinateSpace.UNKNOWN:
                values = [
                    value
                    for element in page.elements
                    for value in element.bbox
                ]
                coordinate_space = (
                    CoordinateSpace.PDF_POINTS
                    if values and max(values) <= max(width, height) * 1.05
                    else CoordinateSpace.NORMALIZED
                )
            for element in page.elements:
                element.bbox = _normalized_bbox(element.bbox, width, height, coordinate_space)
                for fragment in element.visible_fragments:
                    if fragment.bbox:
                        fragment.bbox = _normalized_bbox(
                            fragment.bbox, width, height, coordinate_space
                        )
            for artifact in page.artifacts:
                artifact.bbox = _normalized_bbox(
                    artifact.bbox, width, height, coordinate_space
                )
            page.coordinate_space = CoordinateSpace.NORMALIZED


def _transformation_kind(source: str, replacement: str) -> TransformationKind:
    if "\u00ad" in source:
        return TransformationKind.SOFT_HYPHEN_REMOVAL
    if any(character in source for character in _LIGATURES):
        return TransformationKind.LIGATURE_EXPANSION
    if (
        "-" in source
        and not replacement
        and (
            any(character in source for character in "\r\n")
            or re.fullmatch(r"-\s*", source)
        )
    ):
        return TransformationKind.LINE_BREAK_DEHYPHENATION
    if source and not replacement and not re.sub(r"[\s←→⟵⟶]", "", source):
        return TransformationKind.DECORATIVE_MARKER_OMISSION
    if source.isspace() and replacement in {"; ", ": "}:
        return TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION
    if any(character in _FORMULA_CHARACTERS for character in source + replacement) or re.search(
        r"[\u0370-\u03ff]", source + replacement
    ):
        return TransformationKind.FORMULA_SPOKEN_EQUIVALENT
    if (
        any(character in ".·…_" for character in source)
        and not re.sub(r"[\s.·…_]", "", source)
        and not replacement.strip()
    ):
        return TransformationKind.DECORATIVE_LEADER_OMISSION
    if (not source or source.isspace()) and (not replacement or replacement.isspace()):
        return TransformationKind.WHITESPACE_NORMALIZATION
    return TransformationKind.UNVERIFIED


def _transformation_rationale(kind: TransformationKind) -> str:
    return {
        TransformationKind.LINE_BREAK_DEHYPHENATION: "Joins a word split only by a printed line break.",
        TransformationKind.LIGATURE_EXPANSION: "Expands a presentation ligature to Unicode letters.",
        TransformationKind.SOFT_HYPHEN_REMOVAL: "Removes a discretionary soft hyphen.",
        TransformationKind.FORMULA_SPOKEN_EQUIVALENT: "Supplies a spoken equivalent for mathematical notation.",
        TransformationKind.DECORATIVE_LEADER_OMISSION: "Omits visual leader characters from speech.",
        TransformationKind.DECORATIVE_MARKER_OMISSION: "Omits a visual direction marker that is not needed in linear speech.",
        TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION: "Supplies spoken punctuation between adjacent labeled entries.",
        TransformationKind.WHITESPACE_NORMALIZATION: "Normalizes layout whitespace for continuous reading.",
        TransformationKind.UNVERIFIED: "The textual change is not one of the approved mechanical transformations.",
    }[kind]


def canonicalize_transformations(element: PageElement) -> None:
    """Replace prose transformation claims with exact, reproducible character spans."""
    if element.role == ElementRole.FIGURE:
        element.transformations = []
        return
    visible, accessible = element.visible_text, element.accessible_text
    transformations: list[TextTransformation] = []
    matcher = SequenceMatcher(None, visible, accessible, autojunk=False)
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        source_text = visible[source_start:source_end]
        replacement_text = accessible[target_start:target_end]
        kind = _transformation_kind(source_text, replacement_text)
        transformations.append(
            TextTransformation(
                kind=kind,
                source_text=source_text,
                replacement_text=replacement_text,
                rationale=_transformation_rationale(kind),
                source_start=source_start,
                source_end=source_end,
                target_start=target_start,
                target_end=target_end,
            )
        )
    element.transformations = transformations
    element.tokens = exact_text_tokens(accessible)
    message = "Accessible text contains a change that is not an approved mechanical transformation."
    element.findings = [
        finding
        for finding in element.findings
        if not (
            finding.category == FindingCategory.TRANSFORMATION
            and finding.message == message
        )
    ]
    if any(item.kind == TransformationKind.UNVERIFIED for item in transformations):
        element.findings.append(
            ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.TRANSFORMATION,
                message=message,
                chosen=accessible[:300],
            )
        )


def transformation_errors(plan: DocumentPlan) -> list[str]:
    errors: list[str] = []
    for page in plan.pages:
        for element in page.elements:
            if element.role == ElementRole.FIGURE:
                continue
            cursor = 0
            rebuilt: list[str] = []
            for transformation in element.transformations:
                spans = (
                    transformation.source_start,
                    transformation.source_end,
                    transformation.target_start,
                    transformation.target_end,
                )
                if any(value is None for value in spans):
                    errors.append(f"{element.id}: transformation lacks exact spans")
                    continue
                source_start = int(transformation.source_start)
                source_end = int(transformation.source_end)
                target_start = int(transformation.target_start)
                target_end = int(transformation.target_end)
                if source_start < cursor:
                    errors.append(f"{element.id}: transformation source spans overlap")
                    continue
                if element.visible_text[source_start:source_end] != transformation.source_text:
                    errors.append(f"{element.id}: transformation source span does not match visible text")
                if element.accessible_text[target_start:target_end] != transformation.replacement_text:
                    errors.append(f"{element.id}: transformation target span does not match accessible text")
                rebuilt.append(element.visible_text[cursor:source_start])
                rebuilt.append(transformation.replacement_text)
                cursor = source_end
            rebuilt.append(element.visible_text[cursor:])
            if "".join(rebuilt) != element.accessible_text:
                errors.append(f"{element.id}: transformations do not reconstruct accessible text")
    return errors


def _furniture_fingerprint(element: PageElement) -> str | None:
    top, bottom = element.bbox[1], element.bbox[3]
    if not (bottom <= 100 or top >= 900):
        return None
    text = element.semantic_text.strip().lower()
    if not text or len(text) > 180:
        return None
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[^a-z#]+", " ", text)
    return " ".join(text.split())


def _artifact_reason(
    page_number: int, element: PageElement, repeated_furniture: set[str]
) -> ArtifactReason | None:
    text = element.semantic_text.strip()
    if re.fullmatch(r"(?:page\s*)?0*%d" % page_number, text, flags=re.IGNORECASE):
        return ArtifactReason.PAGE_NUMBER
    if text and not re.search(r"[A-Za-z0-9]", text) and len(text) >= 12:
        return ArtifactReason.WRITING_LINE
    if element.role == ElementRole.FIGURE:
        alt = (element.alt_text or "").strip().lower()
        width = element.bbox[2] - element.bbox[0]
        height = element.bbox[3] - element.bbox[1]
        explicitly_decorative = any(
            term in alt for term in ("decorative", "flourish", "ornament")
        )
        generic_and_small = alt in _GENERIC_ALT_TEXT and (width <= 80 or height <= 25)
        if explicitly_decorative or generic_and_small:
            return ArtifactReason.DECORATION
    fingerprint = _furniture_fingerprint(element)
    if fingerprint and fingerprint in repeated_furniture:
        return ArtifactReason.RUNNING_FURNITURE
    return None


def _fragment_artifact_reason(
    text: str,
    element: PageElement,
) -> ArtifactReason | None:
    if re.fullmatch(r"[\s_.·…-]{12,}", text):
        return ArtifactReason.WRITING_LINE
    stripped = text.strip()
    decorative_kinds = {
        TransformationKind.DECORATIVE_LEADER_OMISSION,
        TransformationKind.DECORATIVE_MARKER_OMISSION,
    }
    if (
        stripped
        and not re.search(r"[A-Za-z0-9]", stripped)
        and stripped not in element.accessible_text
        and any(
            transformation.kind in decorative_kinds
            and stripped in transformation.source_text
            for transformation in element.transformations
        )
    ):
        return ArtifactReason.DECORATION
    return None


def _dedupe_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    result: list[ReviewFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding.severity.value, finding.category.value, " ".join(finding.message.split()))
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result


def _mark_resolved_findings(findings: list[ReviewFinding]) -> None:
    for finding in findings:
        message = finding.message.lower()
        resolved_geometry = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category == FindingCategory.GEOMETRY
            and any(
                term in message
                for term in ("first-model", "first proposal", "proposal", "proposed element")
            )
            and any(
                term in message
                for term in ("coordinate", "geometry", "bbox", "boxes", "image-pixel")
            )
        )
        resolved_furniture = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category == FindingCategory.CONTENT_COVERAGE
            and "running header" in message
            and "page number" in message
        )
        if resolved_geometry:
            finding.severity = ReviewSeverity.INFO
            finding.chosen = "Resolved: canonical geometry is normalized and evidence-derived."
        elif resolved_furniture:
            finding.severity = ReviewSeverity.INFO
            finding.category = FindingCategory.ARTIFACT
            finding.chosen = "Resolved: running furniture remains visible and is explicitly artifacted."


def refine_document_plan(source: Path, plan: DocumentPlan) -> DocumentPlan:
    """Apply deterministic, idempotent corrections after model review."""
    normalize_plan_geometry(source, plan)
    fingerprints = Counter(
        fingerprint
        for page in plan.pages
        for element in page.elements
        if (fingerprint := _furniture_fingerprint(element))
    )
    repeated_furniture = {text for text, count in fingerprints.items() if count >= 2}

    for page in plan.pages:
        kept: list[PageElement] = []
        existing_artifact_keys = {
            (artifact.reason.value, tuple(artifact.bbox), artifact.text) for artifact in page.artifacts
        }
        for element in page.elements:
            reason = _artifact_reason(page.page_number, element, repeated_furniture)
            if reason:
                text = element.visible_text or element.accessible_text or element.alt_text or ""
                key = (reason.value, tuple(element.bbox), text)
                if key not in existing_artifact_keys:
                    page.artifacts.append(
                        ArtifactRecord(reason=reason, bbox=element.bbox, text=text)
                    )
                    existing_artifact_keys.add(key)
                continue

            canonicalize_transformations(element)
            semantic_fragments = []
            for fragment in element.visible_fragments:
                fragment_reason = _fragment_artifact_reason(fragment.text, element)
                if fragment_reason and fragment.bbox:
                    key = (fragment_reason.value, tuple(fragment.bbox), fragment.text)
                    if key not in existing_artifact_keys:
                        page.artifacts.append(
                            ArtifactRecord(
                                reason=fragment_reason,
                                bbox=fragment.bbox,
                                text=fragment.text,
                            )
                        )
                        existing_artifact_keys.add(key)
                    continue
                semantic_fragments.append(fragment)
            element.visible_fragments = semantic_fragments

            if element.role == ElementRole.FIGURE and not (element.alt_text or "").strip():
                message = "Meaningful figure requires reviewed alternate text."
                if not any(finding.message == message for finding in element.findings):
                    element.findings.append(
                        ReviewFinding(
                            severity=ReviewSeverity.CRITICAL,
                            category=FindingCategory.ALT_TEXT,
                            message=message,
                        )
                    )
            _mark_resolved_findings(element.findings)
            element.findings = _dedupe_findings(element.findings)
            kept.append(element)
        page.elements = kept
        page.reconcile_flows()
        _mark_resolved_findings(page.findings)
        page.findings = _dedupe_findings(page.findings)
        for index, artifact in enumerate(page.artifacts, start=1):
            artifact.id = f"p{page.page_number:04d}-a{index:04d}"
    plan.plan_revision = max(plan.plan_revision, 4)
    return plan
