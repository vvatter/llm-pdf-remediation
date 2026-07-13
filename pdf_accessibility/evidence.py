from __future__ import annotations

import re
from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from .extract import PagePacket
from .models import CoordinateSpace, PagePlan


class WordEvidence(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    text: str


class PageEvidence(BaseModel):
    page_number: int
    width: float
    height: float
    coordinate_space: CoordinateSpace = CoordinateSpace.NORMALIZED
    native_text: str
    ocr_text: str
    native_words: list[WordEvidence]
    ocr_words: list[WordEvidence]


class PlanningDiagnostics(BaseModel):
    proposal_native_agreement: float
    proposal_ocr_agreement: float
    sensitive_tokens: list[str]
    proposal_text: str


def evidence_from_packet(packet: PagePacket) -> PageEvidence:
    def normalized(word: tuple[float, float, float, float, str]) -> WordEvidence:
        x0, y0, x1, y1, text = word
        return WordEvidence(
            bbox=[
                x0 / packet.width * 1000,
                y0 / packet.height * 1000,
                x1 / packet.width * 1000,
                y1 / packet.height * 1000,
            ],
            text=text,
        )

    return PageEvidence(
        page_number=packet.page_number,
        width=packet.width,
        height=packet.height,
        native_text=packet.embedded_text,
        ocr_text=packet.ocr_text,
        coordinate_space=CoordinateSpace.NORMALIZED,
        native_words=[normalized(word) for word in packet.native_words],
        ocr_words=[normalized(word) for word in packet.ocr_words],
    )


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text.lower(), flags=re.UNICODE)


def _agreement(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return SequenceMatcher(None, left_tokens, right_tokens, autojunk=False).ratio()


def text_agreement(left: str, right: str) -> float:
    return _agreement(left, right)


def diagnostics_for(proposal: PagePlan, evidence: PageEvidence) -> PlanningDiagnostics:
    proposal_text = "\n".join(element.semantic_text for element in proposal.elements)
    sensitive = sorted(
        set(
            re.findall(
                r"(?:https?://\S+|\b[A-Z][A-Za-z'’-]{2,}\b|\b\d[\d,./:-]*\b|[^\s\w]{2,})",
                proposal_text,
            )
        )
    )
    return PlanningDiagnostics(
        proposal_native_agreement=_agreement(proposal_text, evidence.native_text),
        proposal_ocr_agreement=_agreement(proposal_text, evidence.ocr_text),
        sensitive_tokens=sensitive,
        proposal_text=proposal_text,
    )
