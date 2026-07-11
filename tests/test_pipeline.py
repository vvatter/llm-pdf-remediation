from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path

import pikepdf
import pymupdf

from pdf_accessibility.compiler import _is_column_continuation, compile_tagged_pdf
from pdf_accessibility.models import (
    DocumentPlan,
    ElementRole,
    PageElement,
    PagePlan,
)
from pdf_accessibility.planner import normalize_document_pages
from pdf_accessibility.validate import validate_output


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
    def test_detects_only_incomplete_cross_column_paragraphs(self) -> None:
        previous = element(
            ElementRole.P,
            "This sufficiently long paragraph continues with our plan to open",
        )
        following = element(ElementRole.P, "our group to graduate students")
        left_bottom = (40.0, 700.0, 280.0, 715.0)
        right_top = (330.0, 80.0, 560.0, 95.0)

        self.assertTrue(
            _is_column_continuation(
                previous, following, left_bottom, right_top, 612.0, 792.0
            )
        )

        previous.text += "."
        self.assertFalse(
            _is_column_continuation(
                previous, following, left_bottom, right_top, 612.0, 792.0
            )
        )

        previous.text = "This sufficiently long paragraph ends with an incomplete thought"
        following.text = "But this is visibly a new paragraph."
        self.assertFalse(
            _is_column_continuation(
                previous, following, left_bottom, right_top, 612.0, 792.0
            )
        )

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
            report = validate_output(source, output)

            self.assertTrue(report["visual_match"])
            self.assertTrue(report["qpdf_ok"])
            self.assertTrue(report["fully_tagged"])
            self.assertTrue(report["all_elements_have_accessible_text"])
            self.assertEqual(report["language"], "en-US")
            self.assertGreaterEqual(report["bookmark_count"], 1)
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


if __name__ == "__main__":
    unittest.main()
