from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ElementRole(str, Enum):
    DOCUMENT_TITLE = "DocumentTitle"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    P = "P"
    FIGURE = "Figure"


class PageElement(BaseModel):
    role: ElementRole
    text: str = Field(description="Exact accessible text in reading order; empty only for figures")
    alt_text: str | None = Field(default=None, description="Concise figure alternative text")
    bbox: list[float] = Field(
        min_length=4,
        max_length=4,
        description="Approximate [left, top, right, bottom] in normalized 0..1000 page coordinates"
    )
    confidence: float = Field(ge=0, le=1)
    ambiguity: str | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "PageElement":
        if self.role == ElementRole.FIGURE and not self.alt_text:
            self.alt_text = "Historical newsletter image"
        if self.role != ElementRole.FIGURE and not self.text.strip():
            raise ValueError("non-figure elements require text")
        return self


class PagePlan(BaseModel):
    page_number: int = Field(ge=1)
    elements: list[PageElement] = Field(
        description="Elements in the exact logical reading order for assistive technology"
    )
    page_ambiguities: list[str] = Field(default_factory=list)


class DocumentPlan(BaseModel):
    source_file: str
    title: str
    language: str = "en-US"
    pages: list[PagePlan]
