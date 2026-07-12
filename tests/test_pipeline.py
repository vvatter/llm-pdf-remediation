from __future__ import annotations

import tempfile
import unittest
import subprocess
import json
from types import SimpleNamespace
from pathlib import Path

import pikepdf
import pymupdf

from pdf_accessibility.compiler import compile_tagged_pdf
from pdf_accessibility.models import (
    DocumentPlan,
    ElementRole,
    PageElement,
    PagePlan,
    exact_text_tokens,
)
from pdf_accessibility.plans import load_document_plan
from pdf_accessibility.evidence import diagnostics_for, evidence_from_packet
from pdf_accessibility.extract import PagePacket
from pdf_accessibility.planner import (
    ModelPageElement,
    ModelPagePlan,
    ModelReviewDecision,
    normalize_document_pages,
    propose_page,
    review_page,
)
from pdf_accessibility.validate import (
    add_pdfua_declaration,
    plan_is_approved,
    serialize_structure_tree,
    validate_output,
)


def element(
    role: ElementRole,
    text: str = "",
    alt_text: str | None = None,
    top: float = 100,
) -> PageElement:
    return PageElement(
        role=role,
        text=text,
        alt_text=alt_text,
        bbox=[100, top, 900, top + 100],
        confidence=1,
    )


class NormalizePlanTests(unittest.TestCase):
    def test_keeps_one_title_and_removes_decorative_content(self) -> None:
        pages = [
            PagePlan(
                page_number=1,
                elements=[element(ElementRole.DOCUMENT_TITLE, "Little by Little")],
            ),
            PagePlan(
                page_number=2,
                elements=[
                    element(ElementRole.DOCUMENT_TITLE, "LITTLE BY LITTLE. SPRING 2004."),
                    element(ElementRole.DOCUMENT_TITLE, "Alumni News", top=200),
                    element(ElementRole.FIGURE, alt_text="Small decorative flourish", top=400),
                ],
            ),
        ]

        normalized = normalize_document_pages(Path("newsletter.pdf"), pages)

        self.assertEqual(
            [item.role for page in normalized for item in page.elements],
            [ElementRole.DOCUMENT_TITLE, ElementRole.H1],
        )

    def test_removes_decorative_contents_leaders(self) -> None:
        pages = [
            PagePlan(
                page_number=1,
                elements=[
                    element(
                        ElementRole.P,
                        "INSIDE THIS ISSUE Notes from the Chair . . . . . 1 Faculty Notes . . . . 2",
                    )
                ],
            )
        ]

        normalized = normalize_document_pages(Path("newsletter.pdf"), pages)

        self.assertEqual(
            normalized[0].elements[0].text,
            "INSIDE THIS ISSUE Notes from the Chair 1 Faculty Notes 2",
        )


class CompileTests(unittest.TestCase):
    def test_exact_tokens_preserve_joiners(self) -> None:
        text = "Dr. Smith—yes,\u00a0indeed.\nNext"
        tokens = exact_text_tokens(text)
        rebuilt = "".join(text[token.start : token.actual_end] for token in tokens)

        self.assertEqual(rebuilt, text)
        self.assertEqual([token.text for token in tokens], ["Dr.", "Smith—yes,", "indeed.", "Next"])

    def test_compiler_preserves_rendering_and_adds_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Visible newsletter text")
            document.save(source)
            document.close()

            plan = DocumentPlan(
                source_file=source.name,
                title="Test Newsletter",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.DOCUMENT_TITLE, "Test Newsletter"),
                            element(
                                ElementRole.P,
                                " ".join(["Visible newsletter text"] * 20),
                                top=200,
                            ),
                            element(
                                ElementRole.FIGURE,
                                alt_text="Historical diagram with two labeled curves",
                                top=400,
                            ),
                        ],
                    )
                ],
            )
            compile_tagged_pdf(source, output, plan)
            report = validate_output(source, output, plan)

            self.assertTrue(report["visual_match"])
            self.assertTrue(report["qpdf_ok"])
            self.assertTrue(report["fully_tagged"])
            self.assertTrue(report["all_elements_have_accessible_text"])
            self.assertEqual(report["language"], "en-US")
            self.assertGreaterEqual(report["bookmark_count"], 1)
            self.assertTrue(report["structure_matches_plan"])
            self.assertFalse(report["declares_pdfua"])
            extracted = subprocess.run(
                ["pdftotext", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("Visible newsletter text", extracted)
            self.assertIn(
                "Historical diagram with two labeled curves",
                " ".join(extracted.split()),
            )
            with pikepdf.Pdf.open(output) as pdf:
                self.assertTrue(pdf.Root.MarkInfo.Marked)
                self.assertEqual(str(pdf.Root.Lang), "en-US")
                self.assertEqual(str(pdf.pages[0].obj.Tabs), "/S")
                anchor_font = pdf.pages[0].Resources.Font.A11yAnchor
                self.assertEqual(str(anchor_font.Subtype), "/Type0")
                self.assertIn("/ToUnicode", anchor_font)
                self.assertIn(
                    "/FontFile2",
                    anchor_font.DescendantFonts[0].FontDescriptor,
                )
                paragraph = pdf.Root.StructTreeRoot.K.K[1]
                self.assertGreater(len(paragraph.K), 1)
                self.assertTrue(
                    all(str(content_item.Type) == "/MCR" for content_item in paragraph.K)
                )
                anchor_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertIn(b"/ActualText <FEFF", anchor_stream)
                self.assertEqual(
                    pdf.pages[0].Contents[0].read_bytes(), b"q\n/Artifact BMC\n"
                )
                self.assertEqual(
                    pdf.pages[0].Contents[-2].read_bytes(), b"EMC\nQ\n"
                )

    def test_structure_serializer_preserves_exact_text_and_detects_parent_tree_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            damaged = Path(temp) / "damaged.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Visible")
            document.save(source)
            document.close()
            exact = "Dr. Smith—yes,\u00a0indeed.\nNext"
            plan = DocumentPlan(
                source_file=source.name,
                title="Exact",
                pages=[PagePlan(page_number=1, elements=[element(ElementRole.P, exact)])],
            )
            compile_tagged_pdf(source, output, plan)

            serialized = serialize_structure_tree(output)
            self.assertEqual(serialized["elements"][0]["text"], exact)
            self.assertEqual(serialized["errors"], [])

            with pikepdf.Pdf.open(output) as pdf:
                del pdf.Root.StructTreeRoot.ParentTree
                pdf.save(damaged)
            errors = serialize_structure_tree(damaged)["errors"]
            self.assertTrue(any("ParentTree" in error for error in errors))

    def test_pdfua_declaration_is_added_only_to_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            draft = Path(temp) / "draft.pdf"
            candidate = Path(temp) / "candidate.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(draft)
            document.close()

            add_pdfua_declaration(draft, candidate)

            with pikepdf.Pdf.open(draft) as pdf:
                with pdf.open_metadata() as metadata:
                    self.assertNotIn("pdfuaid:part", metadata)
            with pikepdf.Pdf.open(candidate) as pdf:
                with pdf.open_metadata() as metadata:
                    self.assertEqual(metadata["pdfuaid:part"], "1")


class PlanMigrationTests(unittest.TestCase):
    def test_migrates_v1_plan_and_preserves_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.plan.json"
            path.write_text(
                json.dumps(
                    {
                        "source_file": "sample.pdf",
                        "title": "Sample",
                        "pages": [
                            {
                                "page_number": 1,
                                "elements": [
                                    {
                                        "role": "P",
                                        "text": "Authored sucess.",
                                        "bbox": [0, 0, 1000, 1000],
                                        "confidence": 0.75,
                                        "ambiguity": "unclear spelling",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            plan = load_document_plan(path)

            self.assertEqual(plan.schema_version, 2)
            self.assertEqual(plan.pages[0].elements[0].visible_text, "Authored sucess.")
            self.assertEqual(plan.pages[0].elements[0].accessible_text, "Authored sucess.")
            self.assertTrue(path.with_suffix(".legacy.json").exists())
            self.assertEqual(plan.pages[0].elements[0].id, "p0001-e0001")
            self.assertFalse(plan_is_approved(plan))


class ModelPipelineTests(unittest.TestCase):
    def test_review_receives_image_and_evidence_before_diagnostics_and_proposal(self) -> None:
        packet = PagePacket(
            page_number=1,
            width=612,
            height=792,
            image_data_url="data:image/png;base64,AA==",
            embedded_text="Native evidence",
            ocr_text="OCR evidence",
        )
        evidence = evidence_from_packet(packet)
        model_element = ModelPageElement(
            role=ElementRole.P,
            visible_text="Printed text.",
            accessible_text="Printed text.",
            bbox=[100, 100, 900, 200],
            confidence={
                "transcription": 1,
                "semantic_role": 1,
                "geometry": 1,
                "reading_order": 1,
            },
        )
        model_page = ModelPagePlan(page_number=1, elements=[model_element])

        class FakeResponses:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.outputs = [
                    model_page,
                    ModelReviewDecision(canonical_page=model_page),
                ]

            def parse(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_parsed=self.outputs.pop(0), id="response-id")

        responses = FakeResponses()
        client = SimpleNamespace(responses=responses)
        proposal, _ = propose_page(client, "terra", "medium", packet, evidence)
        diagnostics = diagnostics_for(proposal, evidence)
        decision, _ = review_page(
            client, "sol", "high", packet, evidence, diagnostics, proposal
        )

        self.assertEqual(decision.canonical_page.elements[0].accessible_text, "Printed text.")
        review_content = responses.calls[1]["input"][1]["content"]
        self.assertEqual(review_content[0]["type"], "input_image")
        self.assertTrue(review_content[1]["text"].startswith("EVIDENCE FIRST:"))
        self.assertTrue(review_content[2]["text"].startswith("DETERMINISTIC DIAGNOSTICS:"))
        self.assertTrue(review_content[3]["text"].startswith("FIRST-MODEL PROPOSAL:"))
        self.assertNotIn('"tokens"', review_content[3]["text"])


if __name__ == "__main__":
    unittest.main()
