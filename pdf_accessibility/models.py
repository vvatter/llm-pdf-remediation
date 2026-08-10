from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION = 7
PLAN_REVISION = 11


class ElementRole(str, Enum):
    DOCUMENT_TITLE = "DocumentTitle"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    P = "P"
    LI = "LI"
    TH = "TH"
    TD = "TD"
    FIGURE = "Figure"


class TableHeaderScope(str, Enum):
    ROW = "Row"
    COLUMN = "Column"


class RemediationMode(str, Enum):
    AUTO = "auto"
    PASS_THROUGH = "pass-through"
    NATIVE = "native"
    HYBRID = "hybrid"
    FACSIMILE = "facsimile"
    UNSUPPORTED = "unsupported"


class CoordinateSpace(str, Enum):
    NORMALIZED = "normalized_0_1000"
    PDF_POINTS = "pdf_points"
    UNKNOWN = "unknown"


class ReviewSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ReviewStatus(str, Enum):
    PROPOSAL = "proposal"
    MODEL_REVIEWED = "model_reviewed"
    LEGACY_UNREVIEWED = "legacy_unreviewed"
    MANUAL_MODIFIED = "manual_modified"


class TransformationKind(str, Enum):
    LINE_BREAK_DEHYPHENATION = "line_break_dehyphenation"
    LIGATURE_EXPANSION = "ligature_expansion"
    SOFT_HYPHEN_REMOVAL = "soft_hyphen_removal"
    FORMULA_SPOKEN_EQUIVALENT = "formula_spoken_equivalent"
    DECORATIVE_LEADER_OMISSION = "decorative_leader_omission"
    DECORATIVE_MARKER_OMISSION = "decorative_marker_omission"
    INLINE_LIST_SEPARATOR_OMISSION = "inline_list_separator_omission"
    STRUCTURAL_SEPARATOR_NORMALIZATION = "structural_separator_normalization"
    DATE_RANGE_EXPANSION = "date_range_expansion"
    WHITESPACE_NORMALIZATION = "whitespace_normalization"
    UNVERIFIED = "unverified"


class FindingCategory(str, Enum):
    TRANSCRIPTION = "transcription"
    NAME = "name"
    DATE_NUMBER = "date_number"
    FORMULA = "formula"
    GEOMETRY = "geometry"
    READING_ORDER = "reading_order"
    SEMANTIC_ROLE = "semantic_role"
    CONTINUATION = "continuation"
    DECORATION = "decoration"
    ARTIFACT = "artifact"
    ALT_TEXT = "alt_text"
    EVIDENCE = "evidence"
    TRANSFORMATION = "transformation"
    CONTENT_COVERAGE = "content_coverage"
    LOW_CONFIDENCE = "low_confidence"
    MODEL_DISAGREEMENT = "model_disagreement"
    LEGACY_AMBIGUITY = "legacy_ambiguity"
    PAGE = "page"
    OTHER = "other"


class ArtifactReason(str, Enum):
    PAGE_NUMBER = "page_number"
    RUNNING_FURNITURE = "running_furniture"
    DECORATION = "decoration"
    WRITING_LINE = "writing_line"
    OTHER = "other"


class TextFragment(BaseModel):
    id: str = ""
    text: str
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    evidence_refs: list[str] = Field(default_factory=list)
    alignment_coverage: float | None = Field(default=None, ge=0, le=1)
    geometry_word_count: int = Field(default=0, ge=0)
    geometry_source: str | None = None


def fragment_region_groups(
    fragments: list[TextFragment], proximity: float = 12.0
) -> list[list[int]]:
    """Group visually connected fragments while preserving their semantic order."""
    neighbors = [set([index]) for index in range(len(fragments))]
    for left_index, left_fragment in enumerate(fragments):
        if left_fragment.bbox is None:
            continue
        left, top, right, bottom = left_fragment.bbox
        for right_index in range(left_index + 1, len(fragments)):
            right_fragment = fragments[right_index]
            if right_fragment.bbox is None:
                continue
            other_left, other_top, other_right, other_bottom = right_fragment.bbox
            horizontal_gap = max(0.0, max(left, other_left) - min(right, other_right))
            vertical_gap = max(0.0, max(top, other_top) - min(bottom, other_bottom))
            if horizontal_gap <= proximity and vertical_gap <= proximity:
                neighbors[left_index].add(right_index)
                neighbors[right_index].add(left_index)

    groups: list[list[int]] = []
    visited: set[int] = set()
    for start in range(len(fragments)):
        if start in visited:
            continue
        pending = [start]
        group: list[int] = []
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            group.append(index)
            pending.extend(neighbors[index] - visited)
        groups.append(sorted(group))
    return groups


class PageFlow(BaseModel):
    id: str = ""
    label: str | None = None
    block_ids: list[str] = Field(
        description="Visual block identifiers in exact assistive-technology reading order"
    )


class TextTransformation(BaseModel):
    kind: TransformationKind
    source_text: str
    replacement_text: str
    rationale: str
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    target_start: int | None = Field(default=None, ge=0)
    target_end: int | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class FormulaSpan:
    """Printed notation used for placement plus its reviewed spoken alternative."""

    start: int
    end: int
    text: str
    alt_text: str


def math_extraction_text(text: str) -> str:
    """Normalize mathematical alphabet styling that PDF text engines lose."""
    return "".join(
        unicodedata.normalize("NFKC", character)
        if 0x1D400 <= ord(character) <= 0x1D7FF
        else character
        for character in text
    )


class ConfidenceProfile(BaseModel):
    transcription: float = Field(default=1.0, ge=0, le=1)
    semantic_role: float = Field(default=1.0, ge=0, le=1)
    geometry: float = Field(default=1.0, ge=0, le=1)
    reading_order: float = Field(default=1.0, ge=0, le=1)

    @property
    def minimum(self) -> float:
        return min(
            self.transcription,
            self.semantic_role,
            self.geometry,
            self.reading_order,
        )


class EvidenceMetrics(BaseModel):
    native_agreement: float | None = Field(default=None, ge=0, le=1)
    ocr_agreement: float | None = Field(default=None, ge=0, le=1)
    planner_reviewer_agreement: float | None = Field(default=None, ge=0, le=1)
    alignment_coverage: float | None = Field(default=None, ge=0, le=1)


class ReviewFinding(BaseModel):
    severity: ReviewSeverity
    category: FindingCategory
    message: str
    alternatives: list[str] = Field(default_factory=list)
    chosen: str | None = None
    original_category: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_category(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw = str(data.get("category", "other"))
        normalized = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
        direct = {category.value for category in FindingCategory}
        if normalized in direct:
            data["category"] = normalized
            return data

        if any(term in normalized for term in ("reading", "column")):
            category = FindingCategory.READING_ORDER
        elif any(term in normalized for term in ("geometry", "coordinate")):
            category = FindingCategory.GEOMETRY
        elif any(term in normalized for term in ("role", "caption", "page_type")):
            category = FindingCategory.SEMANTIC_ROLE
        elif any(term in normalized for term in ("formula", "currency", "math")):
            category = FindingCategory.FORMULA
        elif any(term in normalized for term in ("date", "number", "pagination", "address")):
            category = FindingCategory.DATE_NUMBER
        elif any(term in normalized for term in ("name", "place", "institution")):
            category = FindingCategory.NAME
        elif any(term in normalized for term in ("continu", "paragraph_structure")):
            category = FindingCategory.CONTINUATION
        elif any(term in normalized for term in ("decor", "ornament", "footer", "furniture", "running")):
            category = FindingCategory.DECORATION
        elif any(term in normalized for term in ("alt_text", "figure", "graphic")):
            category = FindingCategory.ALT_TEXT
        elif any(term in normalized for term in ("transform", "hyphen", "spacing", "ligature")):
            category = FindingCategory.TRANSFORMATION
        elif any(term in normalized for term in ("evidence", "ocr", "source_reliability")):
            category = FindingCategory.EVIDENCE
        elif any(term in normalized for term in ("omission", "coverage", "content_scope")):
            category = FindingCategory.CONTENT_COVERAGE
        elif any(
            term in normalized
            for term in (
                "transcription",
                "spelling",
                "wording",
                "punctuation",
                "capitalization",
                "abbreviation",
                "typography",
                "glyph",
            )
        ):
            category = FindingCategory.TRANSCRIPTION
        else:
            category = FindingCategory.OTHER
        data["category"] = category.value
        data.setdefault("original_category", raw)
        return data


class ArtifactRecord(BaseModel):
    id: str = ""
    reason: ArtifactReason
    bbox: list[float] = Field(min_length=4, max_length=4)
    text: str = ""


class TextToken(BaseModel):
    text: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    actual_end: int = Field(ge=0)


def exact_text_tokens(text: str) -> list[TextToken]:
    """Return word spans plus the exact joiner following each word."""
    matches = list(re.finditer(r"\S+", text))
    return [
        TextToken(
            text=match.group(0),
            start=match.start(),
            end=match.end(),
            actual_end=matches[index + 1].start() if index + 1 < len(matches) else len(text),
        )
        for index, match in enumerate(matches)
    ]


class PageElement(BaseModel):
    id: str = ""
    role: ElementRole
    visible_fragments: list[TextFragment] = Field(default_factory=list)
    visible_text: str = ""
    accessible_text: str = ""
    transformations: list[TextTransformation] = Field(default_factory=list)
    tokens: list[TextToken] = Field(default_factory=list)
    alt_text: str | None = Field(default=None, description="Concise figure alternative text")
    table_id: str | None = Field(
        default=None,
        description="Stable identifier shared by every cell in one genuine data table",
    )
    table_row: int | None = Field(default=None, ge=0)
    table_column: int | None = Field(default=None, ge=0)
    table_row_span: int | None = Field(default=None, ge=1)
    table_column_span: int | None = Field(default=None, ge=1)
    header_scope: TableHeaderScope | None = None
    bbox: list[float] = Field(
        min_length=4,
        max_length=4,
        description="Approximate [left, top, right, bottom] in normalized 0..1000 page coordinates",
    )
    confidence: ConfidenceProfile = Field(default_factory=ConfidenceProfile)
    evidence: EvidenceMetrics = Field(default_factory=EvidenceMetrics)
    findings: list[ReviewFinding] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PROPOSAL

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_element(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_text = data.pop("text", None)
        if legacy_text is not None:
            data.setdefault("visible_text", legacy_text)
            data.setdefault("accessible_text", legacy_text)
        legacy_confidence = data.get("confidence")
        if isinstance(legacy_confidence, (int, float)):
            data["confidence"] = {
                "transcription": legacy_confidence,
                "semantic_role": legacy_confidence,
                "geometry": legacy_confidence,
                "reading_order": legacy_confidence,
            }
        ambiguity = data.pop("ambiguity", None)
        if ambiguity:
            data.setdefault("findings", []).append(
                {
                    "severity": "warning",
                    "category": "legacy_ambiguity",
                    "message": ambiguity,
                }
            )
        return data

    @model_validator(mode="after")
    def validate_content(self) -> "PageElement":
        if not self.visible_text and self.visible_fragments:
            self.visible_text = "".join(fragment.text for fragment in self.visible_fragments)
        if not self.accessible_text and self.role != ElementRole.FIGURE:
            self.accessible_text = self.visible_text
        if not self.visible_fragments and (self.visible_text or self.role == ElementRole.FIGURE):
            self.visible_fragments = [TextFragment(text=self.visible_text, bbox=self.bbox)]
        if self.role not in {ElementRole.FIGURE, ElementRole.TD} and not self.accessible_text.strip():
            raise ValueError("non-figure elements require accessible text")
        token_text = self.alt_text or "" if self.role == ElementRole.FIGURE else self.accessible_text
        self.tokens = exact_text_tokens(token_text)
        table_roles = {ElementRole.TH, ElementRole.TD}
        if self.role in table_roles:
            if not (self.table_id or "").strip():
                raise ValueError("table cells require table_id")
            if self.table_row is None or self.table_column is None:
                raise ValueError("table cells require zero-based table_row and table_column")
            self.table_row_span = self.table_row_span or 1
            self.table_column_span = self.table_column_span or 1
            if self.role == ElementRole.TH and self.header_scope is None:
                raise ValueError("TH elements require Row or Column header_scope")
            if self.role == ElementRole.TD and self.header_scope is not None:
                raise ValueError("TD elements cannot have header_scope")
        elif any(
            value is not None
            for value in (
                self.table_id,
                self.table_row,
                self.table_column,
                self.table_row_span,
                self.table_column_span,
                self.header_scope,
            )
        ):
            raise ValueError("table metadata is only valid on TH and TD elements")
        return self

    @property
    def minimum_confidence(self) -> float:
        return self.confidence.minimum

    @property
    def semantic_text(self) -> str:
        return self.alt_text or "" if self.role == ElementRole.FIGURE else self.accessible_text

    def _exact_transformations(self) -> list[TextTransformation] | None:
        ordered = sorted(
            self.transformations,
            key=lambda item: (
                -1 if item.source_start is None else item.source_start,
                -1 if item.source_end is None else item.source_end,
            ),
        )
        cursor = 0
        for item in ordered:
            if item.source_start is None or item.source_end is None:
                return None
            if item.source_start < cursor or item.source_end < item.source_start:
                return None
            if self.visible_text[item.source_start : item.source_end] != item.source_text:
                return None
            cursor = item.source_end
        return ordered

    @property
    def extraction_text(self) -> str:
        """Notation-bearing transcript used to align the invisible text layer."""
        if self.role == ElementRole.FIGURE:
            return ""
        ordered = self._exact_transformations()
        if ordered is None:
            return self.visible_text
        if not ordered:
            return self.accessible_text
        cursor = 0
        extracted: list[str] = []
        for item in ordered:
            source_start = int(item.source_start)
            source_end = int(item.source_end)
            extracted.append(self.visible_text[cursor:source_start])
            extracted.append(
                math_extraction_text(item.source_text)
                if item.kind == TransformationKind.FORMULA_SPOKEN_EQUIVALENT
                else item.replacement_text
            )
            cursor = source_end
        extracted.append(self.visible_text[cursor:])
        return "".join(extracted)

    @property
    def formula_spans(self) -> list[FormulaSpan]:
        """Return exact formula ranges in the alignment transcript with reviewed speech."""
        ordered = self._exact_transformations()
        if ordered is None:
            return []
        source_cursor = 0
        extraction_cursor = 0
        spans: list[FormulaSpan] = []
        for item in ordered:
            source_start = int(item.source_start)
            source_end = int(item.source_end)
            extraction_cursor += len(self.visible_text[source_cursor:source_start])
            if item.kind == TransformationKind.FORMULA_SPOKEN_EQUIVALENT:
                notation = math_extraction_text(item.source_text)
                start = extraction_cursor
                extraction_cursor += len(notation)
                spans.append(
                    FormulaSpan(
                        start=start,
                        end=extraction_cursor,
                        text=notation,
                        alt_text=item.replacement_text,
                    )
                )
            else:
                extraction_cursor += len(item.replacement_text)
            source_cursor = source_end
        return spans

    @property
    def text(self) -> str:
        """Compatibility alias for schema-v1 callers."""
        return self.accessible_text

    @text.setter
    def text(self, value: str) -> None:
        self.accessible_text = value
        self.tokens = exact_text_tokens(value)

    @property
    def ambiguity(self) -> str | None:
        return self.findings[0].message if self.findings else None


class FormWidgetPlan(BaseModel):
    widget_index: int = Field(
        ge=0,
        description="Zero-based index in the page's supplied interactive-widget evidence",
    )
    description: str = Field(
        min_length=1,
        max_length=240,
        description="Concise visible-context accessible name for the interactive control",
    )


class PagePlan(BaseModel):
    page_number: int = Field(ge=1)
    document_title_candidate: str | None = Field(default=None, max_length=240)
    coordinate_space: CoordinateSpace = CoordinateSpace.UNKNOWN
    elements: list[PageElement] = Field(
        description="Elements in the exact logical reading order for assistive technology"
    )
    form_widgets: list[FormWidgetPlan] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    flows: list[PageFlow] = Field(default_factory=list)
    page_ambiguities: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PROPOSAL

    @model_validator(mode="after")
    def assign_stable_ids(self) -> "PagePlan":
        widget_indices = [widget.widget_index for widget in self.form_widgets]
        if len(widget_indices) != len(set(widget_indices)):
            raise ValueError(f"page {self.page_number} has duplicate form widget indices")
        closed_tables: set[str] = set()
        active_table: str | None = None
        previous_position: tuple[int, int] | None = None
        block_ids: list[str] = []
        for index, element in enumerate(self.elements, start=1):
            if element.role in {ElementRole.TH, ElementRole.TD}:
                table_id = str(element.table_id)
                if active_table != table_id:
                    if active_table is not None:
                        closed_tables.add(active_table)
                    if table_id in closed_tables:
                        raise ValueError(
                            f"page {self.page_number} table {table_id!r} cells must be consecutive"
                        )
                    active_table = table_id
                    previous_position = None
                position = (int(element.table_row), int(element.table_column))
                if previous_position is not None and position <= previous_position:
                    raise ValueError(
                        f"page {self.page_number} table {table_id!r} cells must be in row-major order"
                    )
                previous_position = position
            elif active_table is not None:
                closed_tables.add(active_table)
                active_table = None
                previous_position = None
            if not element.id:
                element.id = f"p{self.page_number:04d}-e{index:04d}"
            for fragment_index, fragment in enumerate(element.visible_fragments, start=1):
                if not fragment.id:
                    fragment.id = f"{element.id}-b{fragment_index:03d}"
                block_ids.append(fragment.id)
        if len(block_ids) != len(set(block_ids)):
            raise ValueError(f"page {self.page_number} has duplicate visual block identifiers")
        if not self.flows and block_ids:
            self.flows = [
                PageFlow(
                    id=f"p{self.page_number:04d}-flow0001",
                    label="page reading order",
                    block_ids=block_ids,
                )
            ]
        for index, flow in enumerate(self.flows, start=1):
            if not flow.id:
                flow.id = f"p{self.page_number:04d}-flow{index:04d}"
        ordered_blocks = [block_id for flow in self.flows for block_id in flow.block_ids]
        if len(ordered_blocks) != len(set(ordered_blocks)):
            raise ValueError(f"page {self.page_number} assigns a visual block more than once")
        if set(ordered_blocks) != set(block_ids):
            missing = sorted(set(block_ids) - set(ordered_blocks))
            unknown = sorted(set(ordered_blocks) - set(block_ids))
            raise ValueError(
                f"page {self.page_number} flow ownership mismatch; "
                f"missing={missing}, unknown={unknown}"
            )
        if ordered_blocks != block_ids:
            raise ValueError(
                f"page {self.page_number} flow order must match semantic element/fragment order"
            )
        for index, artifact in enumerate(self.artifacts, start=1):
            if not artifact.id:
                artifact.id = f"p{self.page_number:04d}-a{index:04d}"
        return self

    @property
    def block_order(self) -> list[str]:
        return [block_id for flow in self.flows for block_id in flow.block_ids]

    def reconcile_flows(self) -> None:
        """Remove deleted blocks while preserving valid flow partitions and order."""
        block_ids = [
            fragment.id
            for element in self.elements
            for fragment in element.visible_fragments
        ]
        remaining = set(block_ids)
        reconciled = [
            flow.model_copy(
                update={"block_ids": [item for item in flow.block_ids if item in remaining]}
            )
            for flow in self.flows
        ]
        reconciled = [flow for flow in reconciled if flow.block_ids]
        if [item for flow in reconciled for item in flow.block_ids] != block_ids:
            reconciled = (
                [
                    PageFlow(
                        id=f"p{self.page_number:04d}-flow0001",
                        label="page reading order",
                        block_ids=block_ids,
                    )
                ]
                if block_ids
                else []
            )
        self.flows = reconciled


class PageReview(BaseModel):
    page_number: int = Field(ge=1)
    canonical_page: PagePlan
    findings: list[ReviewFinding] = Field(default_factory=list)
    proposal_model: str
    reviewer_model: str
    proposal_response_id: str | None = None
    reviewer_response_id: str | None = None


class DocumentPlan(BaseModel):
    schema_version: int = SCHEMA_VERSION
    source_file: str
    source_sha256: str = ""
    source_page_count: int = Field(default=0, ge=0)
    title: str
    language: str = "en-US"
    pages: list[PagePlan]
    review_status: ReviewStatus = ReviewStatus.MODEL_REVIEWED
    plan_revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_schema(self) -> "DocumentPlan":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported plan schema version {self.schema_version}")
        return self
