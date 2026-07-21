from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import re
import unicodedata

import pymupdf

from .models import (
    ArtifactReason,
    ArtifactRecord,
    CoordinateSpace,
    DocumentPlan,
    ElementRole,
    FindingCategory,
    PageElement,
    PagePlan,
    PLAN_REVISION,
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
_MONTH_DATE_RANGE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+(\d{1,2})–(\d{1,2})\b",
    flags=re.IGNORECASE,
)

_FORMULA_MAPPING_MESSAGE = (
    "Reviewed formula speech could not be mapped atomically to the printed notation."
)


def _find_layout_flexible_span(text: str, needle: str, start: int) -> tuple[int, int] | None:
    direct_start = text.find(needle, start)
    if direct_start >= 0:
        return direct_start, direct_start + len(needle)
    pieces = re.split(r"\s+", needle.strip())
    if len(pieces) < 2:
        return None
    match = re.search(r"\s+".join(re.escape(piece) for piece in pieces), text[start:])
    if match is None:
        return None
    return start + match.start(), start + match.end()


def _looks_mathematical(text: str) -> bool:
    return any(
        character in _FORMULA_CHARACTERS
        or unicodedata.category(character) == "Sm"
        or "\u0370" <= character <= "\u03ff"
        or 0x1D400 <= ord(character) <= 0x1D7FF
        for character in text
    )


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
        and any(character in source for character in "\r\n")
    ):
        return TransformationKind.LINE_BREAK_DEHYPHENATION
    if source and not replacement and not re.sub(r"[\s←→⟵⟶]", "", source):
        return TransformationKind.DECORATIVE_MARKER_OMISSION
    if source.isspace() and replacement in {"; ", ": "}:
        return TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION
    if source.strip() == "|" and replacement.strip() in {",", ";", ":"}:
        return TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION
    if source == "–" and replacement == " to ":
        return TransformationKind.DATE_RANGE_EXPANSION
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
        TransformationKind.DECORATIVE_MARKER_OMISSION: "Omits a reviewed visual marker that is not meaningful in linear speech.",
        TransformationKind.INLINE_LIST_SEPARATOR_OMISSION: "Omits punctuation or a conjunction used only to separate items in a compact visual list.",
        TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION: "Supplies spoken punctuation between adjacent labeled entries.",
        TransformationKind.DATE_RANGE_EXPANSION: "Expands an en dash in a month-and-day range to the spoken word 'to'.",
        TransformationKind.WHITESPACE_NORMALIZATION: "Normalizes layout whitespace for continuous reading.",
        TransformationKind.UNVERIFIED: "The textual change is not one of the approved mechanical transformations.",
    }[kind]


def normalize_spoken_date_ranges(element: PageElement) -> None:
    if element.role == ElementRole.FIGURE:
        return
    accessible = _MONTH_DATE_RANGE.sub(
        lambda match: f"{match.group(1)} {match.group(2)} to {match.group(3)}",
        element.accessible_text,
    )
    if accessible != element.accessible_text:
        element.accessible_text = accessible
        element.tokens = exact_text_tokens(accessible)


def canonicalize_transformations(element: PageElement) -> None:
    """Replace prose transformation claims with exact, reproducible character spans."""
    if element.role == ElementRole.FIGURE:
        element.transformations = []
        return
    marker_free = re.sub(r"^[\s*•●○▪■+\-–—]+", "", element.visible_text)
    if (
        element.accessible_text == element.visible_text
        and marker_free != element.visible_text
        and marker_free
        and element.role
        in {
            ElementRole.DOCUMENT_TITLE,
            ElementRole.H1,
            ElementRole.H2,
            ElementRole.H3,
            ElementRole.P,
            ElementRole.LI,
        }
        and any(
            (finding.chosen or "").strip() == marker_free.strip()
            for finding in element.findings
        )
    ):
        element.accessible_text = marker_free
    visible, accessible = element.visible_text, element.accessible_text
    declared_formulae = [
        item
        for item in element.transformations
        if item.kind == TransformationKind.FORMULA_SPOKEN_EQUIVALENT
    ]
    formula_anchors: list[TextTransformation] = []
    source_cursor = 0
    target_cursor = 0
    invalid_formulae: list[TextTransformation] = []
    for item in declared_formulae:
        if not item.source_text or not item.replacement_text:
            invalid_formulae.append(item)
            continue
        source_span = _find_layout_flexible_span(
            visible, item.source_text, source_cursor
        )
        target_start = accessible.find(item.replacement_text, target_cursor)
        if source_span is None or target_start < 0:
            invalid_formulae.append(item)
            continue
        source_start, source_end = source_span
        target_end = target_start + len(item.replacement_text)
        formula_anchors.append(
            item.model_copy(
                update={
                    "source_text": visible[source_start:source_end],
                    "source_start": source_start,
                    "source_end": source_end,
                    "target_start": target_start,
                    "target_end": target_end,
                }
            )
        )
        source_cursor = source_end
        target_cursor = target_end

    prior_formula_mapping_failure = any(
        finding.message == _FORMULA_MAPPING_MESSAGE for finding in element.findings
    )
    element.findings = [
        finding
        for finding in element.findings
        if finding.message != _FORMULA_MAPPING_MESSAGE
    ]
    if invalid_formulae:
        element.findings.append(
            ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.FORMULA,
                message=_FORMULA_MAPPING_MESSAGE,
                alternatives=[item.source_text for item in invalid_formulae],
                chosen="Retain printed notation until an exact spoken alternative is reviewed.",
            )
        )

    transformations: list[TextTransformation] = []
    reviewed_decorative_omission = any(
        finding.category == FindingCategory.DECORATION
        and any(
            term in f"{finding.message} {finding.chosen or ''}".lower()
            for term in ("omit", "decorative-marker", "ornamental")
        )
        for finding in element.findings
    )
    unmapped_math = False

    def append_gap(
        source_start: int,
        source_end: int,
        target_start: int,
        target_end: int,
    ) -> None:
        nonlocal unmapped_math
        matcher = SequenceMatcher(
            None,
            visible[source_start:source_end],
            accessible[target_start:target_end],
            autojunk=False,
        )
        for tag, local_source_start, local_source_end, local_target_start, local_target_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            exact_source_start = source_start + local_source_start
            exact_source_end = source_start + local_source_end
            exact_target_start = target_start + local_target_start
            exact_target_end = target_start + local_target_end
            source_text = visible[exact_source_start:exact_source_end]
            replacement_text = accessible[exact_target_start:exact_target_end]
            kind = _transformation_kind(source_text, replacement_text)
            if (
                kind == TransformationKind.UNVERIFIED
                and exact_source_start == 0
                and element.role
                in {
                    ElementRole.DOCUMENT_TITLE,
                    ElementRole.H1,
                    ElementRole.H2,
                    ElementRole.H3,
                    ElementRole.P,
                    ElementRole.LI,
                }
                and not replacement_text
                and re.fullmatch(r"[\s*•●○▪■+\-–—]+", source_text)
            ):
                kind = TransformationKind.DECORATIVE_MARKER_OMISSION
            if (
                kind == TransformationKind.UNVERIFIED
                and reviewed_decorative_omission
                and source_text
                and not replacement_text
            ):
                kind = TransformationKind.DECORATIVE_MARKER_OMISSION
            if (
                kind == TransformationKind.UNVERIFIED
                and element.role == ElementRole.LI
                and not replacement_text
                and (
                    source_text.strip() in {",", ";", "|"}
                    or source_text.strip().lower() in {"and", "or", "&"}
                )
            ):
                kind = TransformationKind.INLINE_LIST_SEPARATOR_OMISSION
            if (
                kind == TransformationKind.UNVERIFIED
                and source_text in {"-", "–"}
                and replacement_text == " to "
                and re.search(r"\d\s*$", visible[:exact_source_start])
                and re.match(r"\s*\d", visible[exact_source_end:])
            ):
                kind = TransformationKind.DATE_RANGE_EXPANSION
            if kind == TransformationKind.UNVERIFIED and _looks_mathematical(
                source_text + replacement_text
            ):
                unmapped_math = True
            transformations.append(
                TextTransformation(
                    kind=kind,
                    source_text=source_text,
                    replacement_text=replacement_text,
                    rationale=_transformation_rationale(kind),
                    source_start=exact_source_start,
                    source_end=exact_source_end,
                    target_start=exact_target_start,
                    target_end=exact_target_end,
                )
            )

    source_cursor = 0
    target_cursor = 0
    for anchor in formula_anchors:
        append_gap(
            source_cursor,
            int(anchor.source_start),
            target_cursor,
            int(anchor.target_start),
        )
        transformations.append(anchor)
        source_cursor = int(anchor.source_end)
        target_cursor = int(anchor.target_end)
    append_gap(source_cursor, len(visible), target_cursor, len(accessible))

    if unmapped_math and not invalid_formulae:
        element.findings.append(
            ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.FORMULA,
                message=_FORMULA_MAPPING_MESSAGE,
                chosen="Retain printed notation until an exact spoken alternative is reviewed.",
            )
        )
    elif (
        prior_formula_mapping_failure
        and not invalid_formulae
        and not formula_anchors
    ):
        element.findings.append(
            ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.FORMULA,
                message=_FORMULA_MAPPING_MESSAGE,
                chosen="Retain printed notation until an exact spoken alternative is reviewed.",
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
        cursor = 0
        repaired: list[str] = []
        for item in transformations:
            source_start = int(item.source_start or 0)
            source_end = int(item.source_end or source_start)
            repaired.append(visible[cursor:source_start])
            repaired.append(
                item.source_text
                if item.kind == TransformationKind.UNVERIFIED
                else item.replacement_text
            )
            cursor = source_end
        repaired.append(visible[cursor:])
        element.accessible_text = "".join(repaired)
        repair_message = (
            "Unapproved accessibility-only text changes were reverted to the printed source."
        )
        if not any(finding.message == repair_message for finding in element.findings):
            element.findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.INFO,
                    category=FindingCategory.TRANSFORMATION,
                    message=repair_message,
                    chosen=element.accessible_text[:300],
                )
            )
        canonicalize_transformations(element)


def transformation_errors(plan: DocumentPlan) -> list[str]:
    errors: list[str] = []
    for page in plan.pages:
        for element in page.elements:
            if element.role == ElementRole.FIGURE:
                continue
            cursor = 0
            rebuilt: list[str] = []
            for transformation in element.transformations:
                if (
                    transformation.kind
                    == TransformationKind.FORMULA_SPOKEN_EQUIVALENT
                    and (
                        not transformation.source_text.strip()
                        or not transformation.replacement_text.strip()
                    )
                ):
                    errors.append(
                        f"{element.id}: formula transformation requires notation and spoken text"
                    )
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
        reviewed_decoration = (
            not alt
            and any(
                finding.category == FindingCategory.DECORATION
                for finding in element.findings
            )
        )
        if explicitly_decorative or generic_and_small or reviewed_decoration:
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


def _order_first_page_epigraph(page: PagePlan) -> None:
    if page.page_number != 1:
        return
    first_heading = next(
        (
            index
            for index, element in enumerate(page.elements)
            if element.role in {ElementRole.H1, ElementRole.H2, ElementRole.H3}
        ),
        len(page.elements),
    )
    preamble = page.elements[:first_heading]
    epigraphs = [
        element
        for element in preamble
        if element.role == ElementRole.P
        and re.search(r"[.!?][\"'”’]?\s*[–—]\s*\S", element.semantic_text)
    ]
    if not epigraphs:
        return
    ordinary = [element for element in preamble if element not in epigraphs]
    reordered = [*ordinary, *epigraphs]
    if reordered == preamble:
        return
    page.elements = [*reordered, *page.elements[first_heading:]]
    page.findings.append(
        ReviewFinding(
            severity=ReviewSeverity.INFO,
            category=FindingCategory.READING_ORDER,
            message=(
                "Deterministic masthead order places an attributed epigraph after "
                "publication and issue metadata."
            ),
            chosen="Title, publication descriptor, issue metadata, epigraph, article content.",
        )
    )


def _normalize_isolated_list_items(page: PagePlan) -> None:
    marker = re.compile(r"^(?:[•◦▪‣*]|[-–—]|\(?\d+[.)]|[A-Za-z][.)])\s+")
    for index, element in enumerate(page.elements):
        if element.role != ElementRole.LI:
            continue
        previous_is_item = index > 0 and page.elements[index - 1].role == ElementRole.LI
        next_is_item = (
            index + 1 < len(page.elements)
            and page.elements[index + 1].role == ElementRole.LI
        )
        if previous_is_item or next_is_item or marker.match(element.semantic_text.strip()):
            continue
        element.role = ElementRole.P
        message = (
            "An isolated unmarked entry was normalized from LI to P to avoid a "
            "one-item list announcement."
        )
        if not any(finding.message == message for finding in element.findings):
            element.findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.INFO,
                    category=FindingCategory.SEMANTIC_ROLE,
                    message=message,
                    chosen="P",
                )
            )


def _mark_resolved_findings(findings: list[ReviewFinding]) -> None:
    for finding in findings:
        message = finding.message.lower()
        resolved_geometry = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category == FindingCategory.GEOMETRY
            and any(
                term in message
                for term in (
                    "first-model",
                    "first model",
                    "first proposal",
                    "proposal",
                    "proposed element",
                )
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
        resolved_coverage = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category == FindingCategory.CONTENT_COVERAGE
            and any(term in message for term in ("first-model", "first model", "proposal"))
            and any(term in message for term in ("omitted", "missing"))
            and any(term in message for term in ("canonical", "restores", "include"))
            and bool(finding.chosen)
        )
        resolved_model_role = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category == FindingCategory.SEMANTIC_ROLE
            and any(term in message for term in ("first-model", "first proposal", "proposal"))
            and (
                any(term in message for term in ("canonical plan", "canonical review"))
                or (
                    "image" in message
                    and any(term in message for term in ("align", "places", "supports"))
                )
                or any(term in message for term in ("segmentation", "not separate printed"))
            )
            and bool(finding.chosen)
        )
        resolved_evidence_disagreement = (
            finding.severity == ReviewSeverity.CRITICAL
            and finding.category
            in {
                FindingCategory.NAME,
                FindingCategory.TRANSCRIPTION,
                FindingCategory.DATE_NUMBER,
            }
            and any(term in message for term in ("first-model", "first model", "proposal"))
            and "image" in message
            and any(term in message for term in ("authoritative", "evidence", "read", "supports"))
            and not any(term in message for term in ("uncertain", "ambiguous", "obscured"))
            and bool(finding.chosen)
        )
        if resolved_geometry:
            finding.severity = ReviewSeverity.INFO
            finding.chosen = "Resolved: canonical geometry is normalized and evidence-derived."
        elif resolved_furniture:
            finding.severity = ReviewSeverity.INFO
            finding.category = FindingCategory.ARTIFACT
            finding.chosen = "Resolved: running furniture remains visible and is explicitly artifacted."
        elif resolved_coverage:
            finding.severity = ReviewSeverity.INFO
            finding.category = FindingCategory.MODEL_DISAGREEMENT
            finding.chosen = f"Resolved in canonical plan: {finding.chosen}"
        elif resolved_model_role:
            finding.severity = ReviewSeverity.INFO
            finding.category = FindingCategory.MODEL_DISAGREEMENT
            finding.chosen = f"Resolved in canonical plan: {finding.chosen}"
        elif resolved_evidence_disagreement:
            finding.severity = ReviewSeverity.INFO
            finding.category = FindingCategory.MODEL_DISAGREEMENT
            finding.chosen = f"Resolved from authoritative page evidence: {finding.chosen}"


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
        _normalize_isolated_list_items(page)
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

            normalize_spoken_date_ranges(element)
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
        _order_first_page_epigraph(page)
        page.reconcile_flows()
        _mark_resolved_findings(page.findings)
        page.findings = _dedupe_findings(page.findings)
        for index, artifact in enumerate(page.artifacts, start=1):
            artifact.id = f"p{page.page_number:04d}-a{index:04d}"
    plan.plan_revision = max(plan.plan_revision, PLAN_REVISION)
    return plan
