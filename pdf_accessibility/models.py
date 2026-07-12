from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION = 2


class ElementRole(str, Enum):
    DOCUMENT_TITLE = "DocumentTitle"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    P = "P"
    FIGURE = "Figure"


class RemediationMode(str, Enum):
    AUTO = "auto"
    PASS_THROUGH = "pass-through"
    NATIVE = "native"
    FACSIMILE = "facsimile"
    UNSUPPORTED = "unsupported"


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
    WHITESPACE_NORMALIZATION = "whitespace_normalization"


class TextFragment(BaseModel):
    text: str
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    evidence_refs: list[str] = Field(default_factory=list)


class TextTransformation(BaseModel):
    kind: TransformationKind
    source_text: str
    replacement_text: str
    rationale: str


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
    category: str
    message: str
    alternatives: list[str] = Field(default_factory=list)
    chosen: str | None = None


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
        if not self.visible_fragments and self.visible_text:
            self.visible_fragments = [TextFragment(text=self.visible_text, bbox=self.bbox)]
        if self.role == ElementRole.FIGURE and not self.alt_text:
            self.alt_text = "Historical document image"
        if self.role != ElementRole.FIGURE and not self.accessible_text.strip():
            raise ValueError("non-figure elements require accessible text")
        token_text = self.alt_text or "" if self.role == ElementRole.FIGURE else self.accessible_text
        self.tokens = exact_text_tokens(token_text)
        return self

    @property
    def minimum_confidence(self) -> float:
        return self.confidence.minimum

    @property
    def semantic_text(self) -> str:
        return self.alt_text or "" if self.role == ElementRole.FIGURE else self.accessible_text

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


class PagePlan(BaseModel):
    page_number: int = Field(ge=1)
    elements: list[PageElement] = Field(
        description="Elements in the exact logical reading order for assistive technology"
    )
    page_ambiguities: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PROPOSAL

    @model_validator(mode="after")
    def assign_stable_ids(self) -> "PagePlan":
        for index, element in enumerate(self.elements, start=1):
            if not element.id:
                element.id = f"p{self.page_number:04d}-e{index:04d}"
        return self


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
