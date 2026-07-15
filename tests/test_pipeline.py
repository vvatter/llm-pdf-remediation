from __future__ import annotations

import tempfile
import unittest
import subprocess
import json
from types import SimpleNamespace
from pathlib import Path

import pikepdf
import pymupdf
from pydantic import ValidationError

from pdf_accessibility.compiler import (
    _align_element_fragments,
    _page_anchor_chunks,
    compile_tagged_pdf,
)
from pdf_accessibility.models import (
    ArtifactReason,
    CoordinateSpace,
    DocumentPlan,
    ElementRole,
    FindingCategory,
    PageElement,
    PageFlow,
    PagePlan,
    ReviewFinding,
    ReviewSeverity,
    ReviewStatus,
    TextFragment,
    TransformationKind,
    exact_text_tokens,
)
from pdf_accessibility.plans import load_document_plan
from pdf_accessibility.evidence import diagnostics_for, evidence_from_packet
from pdf_accessibility.extract import PagePacket
from pdf_accessibility.planner import (
    _validated_page_plan,
    ModelPageElement,
    ModelPagePlan,
    ModelReviewDecision,
    normalize_document_pages,
    propose_page,
    review_page,
)
from pdf_accessibility.refine import refine_document_plan, transformation_errors
from pdf_accessibility.validate import (
    add_pdfua_declaration,
    compare_structure_to_plan,
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
    def test_invalid_model_flow_is_rebuilt_from_semantic_order(self) -> None:
        page = PagePlan(
            page_number=9,
            elements=[
                element(ElementRole.P, "First"),
                element(ElementRole.P, "Second", top=200),
            ],
        )
        page_data = page.model_dump(mode="json")
        page_data["flows"][0]["block_ids"].reverse()

        normalized = _validated_page_plan(page_data, 9)

        self.assertEqual(
            normalized.block_order,
            [
                fragment.id
                for item in normalized.elements
                for fragment in item.visible_fragments
            ],
        )
        self.assertTrue(
            any(
                finding.category == FindingCategory.READING_ORDER
                and "deterministic parser" in finding.message
                for finding in normalized.findings
            )
        )

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
            report = validate_output(source, output, plan, reference_source=source)

            self.assertTrue(report["visual_match"])
            self.assertTrue(report["qpdf_ok"])
            self.assertTrue(report["fully_tagged"])
            self.assertTrue(report["all_elements_have_accessible_text"])
            self.assertEqual(report["language"], "en-US")
            self.assertGreaterEqual(report["bookmark_count"], 1)
            self.assertTrue(report["structure_matches_plan"])
            self.assertTrue(report["block_plan_valid"])
            self.assertFalse(report["declares_pdfua"])
            self.assertFalse(report["extraction_compatible"])
            self.assertTrue(report["transformations_valid"])
            self.assertTrue(report["source_visual_fidelity_ok"])
            extracted = subprocess.run(
                ["pdftotext", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("Visible newsletter text", extracted)
            self.assertNotIn("Historical diagram", extracted)
            with pikepdf.Pdf.open(output) as pdf:
                self.assertTrue(pdf.Root.MarkInfo.Marked)
                self.assertEqual(str(pdf.Root.Lang), "en-US")
                self.assertEqual(str(pdf.pages[0].obj.Tabs), "/S")
                self.assertIn("/PageLabels", pdf.Root)
                anchor_font = pdf.pages[0].Resources.Font.A11yAnchor
                self.assertEqual(str(anchor_font.Subtype), "/Type0")
                self.assertIn("/ToUnicode", anchor_font)
                self.assertIn(
                    "/FontFile2",
                    anchor_font.DescendantFonts[0].FontDescriptor,
                )
                paragraph = pdf.Root.StructTreeRoot.K.K[1]
                self.assertEqual(str(paragraph.S), "/P")
                self.assertNotIn("/ID", paragraph)
                self.assertEqual(str(paragraph.A.O), "/Layout")
                self.assertEqual(len(paragraph.A.BBox), 4)
                self.assertEqual(int(paragraph.K), 1)
                figure = pdf.Root.StructTreeRoot.K.K[2]
                self.assertEqual(str(figure.S), "/Figure")
                self.assertEqual(
                    str(figure.Alt), "Historical diagram with two labeled curves"
                )
                self.assertEqual(str(figure.A.O), "/Layout")
                self.assertEqual(len(figure.A.BBox), 4)
                anchor_streams = [
                    stream.read_bytes() for stream in pdf.pages[0].Contents[-3:]
                ]
                self.assertTrue(
                    anchor_streams[0].startswith(b"/H1 <</MCID 0>> BDC\nBT\n")
                )
                self.assertTrue(
                    all(stream.count(b"BT\n") == 1 for stream in anchor_streams[:2])
                )
                self.assertTrue(
                    all(stream.count(b"ET\n") == 1 for stream in anchor_streams[:2])
                )
                self.assertTrue(anchor_streams[2].startswith(b"/Figure <</MCID 2>> BDC\nq\n"))
                self.assertNotIn(b"BT\n", anchor_streams[2])
                self.assertNotIn(b" Tj", anchor_streams[2])
                self.assertNotIn(b"/ActualText", b"".join(anchor_streams))
                self.assertEqual(
                    pdf.pages[0].Contents[0].read_bytes(), b"q\n/Artifact BMC\n"
                )
                self.assertEqual(
                    pdf.pages[0].Contents[-4].read_bytes(), b"EMC\nQ\n"
                )

    def test_fragment_alignment_is_local_when_page_words_are_interleaved(self) -> None:
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(
                    id="left",
                    text="one two ",
                    bbox=[0, 0, 450, 1000],
                ),
                TextFragment(
                    id="right",
                    text="three four",
                    bbox=[550, 0, 1000, 1000],
                ),
            ],
            visible_text="one two three four",
            accessible_text="one two three four",
            bbox=[0, 0, 1000, 1000],
        )
        chunks = _page_anchor_chunks([planned])[0]
        globally_interleaved_words = [
            (5.0, 5.0, 20.0, 15.0, "one"),
            (60.0, 5.0, 80.0, 15.0, "three"),
            (5.0, 25.0, 20.0, 35.0, "two"),
            (60.0, 25.0, 80.0, 35.0, "four"),
        ]

        placements = _align_element_fragments(
            planned,
            chunks,
            globally_interleaved_words,
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertTrue(all(item.bbox[2] <= 45 for mcid in (0, 1) for item in placements[mcid]))
        self.assertTrue(all(item.bbox[0] >= 55 for mcid in (2, 3) for item in placements[mcid]))
        self.assertEqual(
            [fragment.geometry_word_count for fragment in planned.visible_fragments],
            [2, 2],
        )
        self.assertTrue(
            all(fragment.alignment_coverage == 1 for fragment in planned.visible_fragments)
        )

    def test_multiblock_paragraph_compiles_as_direct_clickable_regions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=100, height=100)
            document.save(source)
            document.close()
            paragraph = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(
                        text="First block ",
                        bbox=[0, 500, 400, 900],
                    ),
                    TextFragment(
                        text="second block",
                        bbox=[500, 0, 900, 400],
                    ),
                ],
                visible_text="First block second block",
                accessible_text="First block second block",
                bbox=[0, 0, 900, 900],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Regions",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan)

            with pikepdf.Pdf.open(output) as pdf:
                regions = list(pdf.Root.StructTreeRoot.K.K)
                self.assertEqual([str(region.S) for region in regions], ["/P", "/P"])
                self.assertTrue(all("/ID" not in region for region in regions))
                self.assertEqual([int(region.K) for region in regions], [0, 1])
                self.assertTrue(all(str(region.A.O) == "/Layout" for region in regions))
                first_box = [float(value) for value in regions[0].A.BBox]
                second_box = [float(value) for value in regions[1].A.BBox]
                self.assertLessEqual(first_box[2], second_box[0])
                anchor_streams = [
                    stream.read_bytes() for stream in pdf.pages[0].Contents[-2:]
                ]
                self.assertTrue(
                    all(stream.count(b"BT\n") == 1 for stream in anchor_streams)
                )
                self.assertTrue(
                    all(stream.count(b"ET\n") == 1 for stream in anchor_streams)
                )
                self.assertTrue(
                    all(b"/ActualText" not in stream for stream in anchor_streams)
                )
            serialized = serialize_structure_tree(output)
            self.assertEqual(
                [record["text"] for record in serialized["elements"]],
                ["First block ", "second block"],
            )
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])

    def test_interleaved_multiblock_text_remains_one_ordered_region(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=100, height=100)
            document.save(source)
            document.close()
            paragraph = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(
                        text="Chair",
                        bbox=[0, 0, 400, 200],
                    ),
                    TextFragment(
                        text="Joseph Glover",
                        bbox=[500, 0, 900, 200],
                    ),
                    TextFragment(
                        text="Editor",
                        bbox=[0, 210, 400, 400],
                    ),
                    TextFragment(
                        text="Paul Ehrlich",
                        bbox=[500, 210, 900, 400],
                    ),
                ],
                visible_text="Chair Joseph Glover. Editor Paul Ehrlich.",
                accessible_text="Chair Joseph Glover. Editor Paul Ehrlich.",
                bbox=[0, 0, 900, 900],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Interleaved regions",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan)

            serialized = serialize_structure_tree(output)
            self.assertEqual(len(serialized["elements"]), 1)
            self.assertEqual(
                serialized["elements"][0]["text"],
                "Chair Joseph Glover. Editor Paul Ehrlich.",
            )
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])

    def test_direct_unicode_stream_uses_ocr_line_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=300, height=200)
            document.save(source)
            document.close()
            document = pymupdf.open()
            page = document.new_page(width=300, height=200)
            page.insert_text((30, 50), "First semantic line")
            page.insert_text((30, 75), "Second semantic line")
            document.save(geometry)
            document.close()
            paragraph = PageElement(
                role=ElementRole.P,
                visible_text="First semantic line Second semantic line",
                accessible_text="First semantic line Second semantic line",
                bbox=[50, 100, 950, 500],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Line geometry",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan, geometry_source=geometry)

            with pikepdf.Pdf.open(output) as pdf:
                anchor_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertEqual(anchor_stream.count(b" Tj\n"), 2)
                self.assertNotIn(b"/ActualText", anchor_stream)
                self.assertEqual(anchor_stream.count(b" Tm "), 2)
            with pymupdf.open(output) as document:
                self.assertEqual(
                    [word[4] for word in document[0].get_text("words", sort=False)],
                    ["First", "semantic", "line", "Second", "semantic", "line"],
                )
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )

    def test_low_agreement_title_uses_reviewed_region_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=300, height=200)
            document.save(source)
            document.close()
            document = pymupdf.open()
            page = document.new_page(width=300, height=200)
            page.insert_text((30, 40), "LIT TLE")
            document.save(geometry)
            document.close()
            title = PageElement(
                role=ElementRole.DOCUMENT_TITLE,
                visible_text="LITTLE\nby\nlittle",
                accessible_text="LITTLE by little",
                bbox=[50, 50, 950, 300],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="LITTLE by little",
                pages=[PagePlan(page_number=1, elements=[title])],
            )

            compile_tagged_pdf(source, output, plan, geometry_source=geometry)

            with pikepdf.Pdf.open(output) as pdf:
                anchor_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertEqual(anchor_stream.count(b" Tj\n"), 1)
            with pymupdf.open(output) as document:
                words = document[0].get_text("words", sort=False)
                self.assertEqual([word[4] for word in words], ["LITTLE", "by", "little"])
                self.assertEqual(len({(word[5], word[6]) for word in words}), 1)

    def test_long_low_evidence_line_preserves_poppler_word_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=300, height=200)
            document.save(source)
            document.close()
            text = (
                "The academic year 2003-2004 was another eventful year highlighted "
                "by the special year in applied mathematics."
            )
            paragraph = PageElement(
                role=ElementRole.P,
                visible_text=text,
                accessible_text=text,
                bbox=[100, 100, 900, 300],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan)

            extracted = subprocess.run(
                ["pdftotext", "-raw", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("The academic year 2003-2004", " ".join(extracted.split()))

    def test_drop_cap_fragments_remain_one_paragraph_region(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=100, height=100)
            document.save(source)
            document.close()
            paragraph = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(text="T", bbox=[50, 100, 90, 300]),
                    TextFragment(
                        text="he paragraph continues.",
                        bbox=[50, 120, 900, 320],
                    ),
                ],
                visible_text="The paragraph continues.",
                accessible_text="The paragraph continues.",
                bbox=[50, 100, 900, 320],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Drop cap",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan)

            with pikepdf.Pdf.open(output) as pdf:
                regions = list(pdf.Root.StructTreeRoot.K.K)
                self.assertEqual(len(regions), 1)
                self.assertNotIn("/ID", regions[0])
                self.assertEqual(int(regions[0].K), 0)
                anchor_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertEqual(anchor_stream.count(b"BT\n"), 1)
                self.assertEqual(anchor_stream.count(b"ET\n"), 1)
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )

    def test_invalid_legacy_fragment_box_is_repaired_from_matching_words(self) -> None:
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(
                    text="Professor Smith",
                    bbox=[700, 1000, 1000, 1000],
                )
            ],
            visible_text="Professor Smith",
            accessible_text="Professor Smith",
            bbox=[700, 1000, 1000, 1000],
        )
        chunks = _page_anchor_chunks([planned])[0]

        placements = _align_element_fragments(
            planned,
            chunks,
            [
                (60.0, 80.0, 75.0, 90.0, "Professor"),
                (77.0, 80.0, 90.0, 90.0, "Smith"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertEqual(len(placements), 2)
        left, top, right, bottom = planned.visible_fragments[0].bbox
        self.assertTrue(0 <= left < right <= 1000)
        self.assertTrue(0 <= top < bottom <= 1000)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)

    def test_structure_serializer_preserves_exact_text_and_detects_parent_tree_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            damaged = Path(temp) / "damaged.pdf"
            identified = Path(temp) / "identified-without-id-tree.pdf"
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
            self.assertEqual(serialized["elements"][0]["id"], "")
            self.assertEqual(serialized["errors"], [])
            with pikepdf.Pdf.open(output) as pdf:
                direct_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertNotIn(b"/ActualText", direct_stream)
                self.assertIn(b"000A", direct_stream)

            with pikepdf.Pdf.open(output) as pdf:
                del pdf.Root.StructTreeRoot.ParentTree
                pdf.save(damaged)
            errors = serialize_structure_tree(damaged)["errors"]
            self.assertTrue(any("ParentTree" in error for error in errors))

            with pikepdf.Pdf.open(output) as pdf:
                pdf.Root.StructTreeRoot.K.K[0][pikepdf.Name.ID] = pikepdf.String(
                    "element-1"
                )
                pdf.save(identified)
            errors = serialize_structure_tree(identified)["errors"]
            self.assertTrue(any("IDTree" in error for error in errors))

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

            self.assertEqual(plan.schema_version, 4)
            self.assertEqual(plan.pages[0].elements[0].visible_text, "Authored sucess.")
            self.assertEqual(plan.pages[0].elements[0].accessible_text, "Authored sucess.")
            self.assertTrue(path.with_suffix(".legacy.json").exists())
            self.assertEqual(plan.pages[0].elements[0].id, "p0001-e0001")
            self.assertFalse(plan_is_approved(plan))

    def test_migrates_reviewed_v2_geometry_without_revoking_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reviewed.plan.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source_file": "sample.pdf",
                        "title": "Sample",
                        "review_status": "model_reviewed",
                        "pages": [
                            {
                                "page_number": 1,
                                "review_status": "model_reviewed",
                                "elements": [
                                    {
                                        "role": "P",
                                        "visible_text": "Text",
                                        "accessible_text": "Text",
                                        "bbox": [36, 72, 200, 90],
                                        "review_status": "model_reviewed",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            plan = load_document_plan(path)

            self.assertEqual(plan.schema_version, 4)
            self.assertEqual(plan.pages[0].coordinate_space, CoordinateSpace.PDF_POINTS)
            self.assertTrue(plan_is_approved(plan))

    def test_visual_blocks_must_have_single_flow_ownership(self) -> None:
        with self.assertRaisesRegex(ValidationError, "more than once"):
            PagePlan(
                page_number=1,
                elements=[
                    PageElement(
                        role=ElementRole.P,
                        visible_fragments=[
                            TextFragment(id="b001", text="First", bbox=[0, 0, 400, 400]),
                            TextFragment(id="b002", text="Second", bbox=[500, 0, 900, 400]),
                        ],
                        visible_text="First Second",
                        accessible_text="First Second",
                        bbox=[0, 0, 900, 400],
                    )
                ],
                flows=[PageFlow(id="flow1", block_ids=["b001", "b001"])],
            )


class DeterministicRefinementTests(unittest.TestCase):
    def test_moves_first_page_epigraph_after_issue_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.DOCUMENT_TITLE, "LITTLE by little", top=20),
                            element(
                                ElementRole.P,
                                "The essence of mathematics resides in its freedom. – Georg Cantor",
                                top=60,
                            ),
                            element(ElementRole.P, "THE NEWSLETTER OF THE DEPARTMENT", top=80),
                            element(ElementRole.P, "VOLUME 20, ISSUE 1, SPRING 2007", top=90),
                            element(ElementRole.H1, "REPORT FROM THE CHAIR", top=120),
                        ],
                    )
                ],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                [item.semantic_text for item in plan.pages[0].elements],
                [
                    "LITTLE by little",
                    "THE NEWSLETTER OF THE DEPARTMENT",
                    "VOLUME 20, ISSUE 1, SPRING 2007",
                    "The essence of mathematics resides in its freedom. – Georg Cantor",
                    "REPORT FROM THE CHAIR",
                ],
            )
            self.assertEqual(
                plan.pages[0].block_order,
                [
                    fragment.id
                    for item in plan.pages[0].elements
                    for fragment in item.visible_fragments
                ],
            )
            self.assertTrue(
                any(
                    finding.category == FindingCategory.READING_ORDER
                    for finding in plan.pages[0].findings
                )
            )

    def test_normalizes_geometry_and_records_repeated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()

            footer = "Little by Little, Department of Mathematics, Volume 20, Spring 2007"
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[
                    PagePlan(
                        page_number=1,
                        coordinate_space=CoordinateSpace.PDF_POINTS,
                        elements=[
                            element(ElementRole.P, "Body", top=100),
                            PageElement(role=ElementRole.P, text="1", bbox=[36, 765, 43, 776]),
                            PageElement(role=ElementRole.P, text=footer, bbox=[72, 765, 500, 776]),
                            PageElement(
                                role=ElementRole.FIGURE,
                                alt_text="Historical document image",
                                bbox=[558, 658, 576, 672],
                            ),
                        ],
                    ),
                    PagePlan(
                        page_number=2,
                        coordinate_space=CoordinateSpace.PDF_POINTS,
                        elements=[
                            element(ElementRole.P, "More body", top=100),
                            PageElement(role=ElementRole.P, text="2", bbox=[36, 765, 43, 776]),
                            PageElement(role=ElementRole.P, text=footer, bbox=[72, 765, 500, 776]),
                            PageElement(role=ElementRole.P, text=". . . . . . . .", bbox=[72, 300, 500, 340]),
                        ],
                    ),
                ],
            )

            refine_document_plan(source, plan)

            self.assertTrue(
                all(page.coordinate_space == CoordinateSpace.NORMALIZED for page in plan.pages)
            )
            reasons = [artifact.reason for page in plan.pages for artifact in page.artifacts]
            self.assertEqual(reasons.count(ArtifactReason.PAGE_NUMBER), 2)
            self.assertEqual(reasons.count(ArtifactReason.RUNNING_FURNITURE), 2)
            self.assertIn(ArtifactReason.DECORATION, reasons)
            self.assertIn(ArtifactReason.WRITING_LINE, reasons)
            self.assertTrue(
                all(0 <= value <= 1000 for page in plan.pages for element in page.elements for value in element.bbox)
            )

    def test_moves_decoration_only_fragment_out_of_semantic_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            form = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(text="Address:", bbox=[100, 100, 300, 150]),
                    TextFragment(text="________________", bbox=[100, 160, 500, 180]),
                ],
                visible_text="Address:\n________________",
                accessible_text="Address:",
                bbox=[100, 100, 500, 180],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[PagePlan(page_number=1, elements=[form])],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                [fragment.text for fragment in form.visible_fragments], ["Address:"]
            )
            self.assertEqual(len(plan.pages[0].block_order), 1)
            self.assertEqual(
                plan.pages[0].artifacts[0].reason, ArtifactReason.WRITING_LINE
            )

    def test_moves_reviewed_empty_alt_flourish_to_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            flourish = PageElement(
                role=ElementRole.FIGURE,
                alt_text=None,
                bbox=[900, 900, 940, 920],
                findings=[
                    ReviewFinding(
                        severity=ReviewSeverity.INFO,
                        category=FindingCategory.DECORATION,
                        message="The small flourish is decorative.",
                        chosen="Retain as an unlabelled decorative figure.",
                    )
                ],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[PagePlan(page_number=1, elements=[flourish])],
            )

            refine_document_plan(source, plan)

            self.assertEqual(plan.pages[0].elements, [])
            self.assertEqual(
                plan.pages[0].artifacts[0].reason, ArtifactReason.DECORATION
            )

    def test_moves_omitted_pointer_fragment_to_decoration_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            caption = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(text="⟶", bbox=[100, 100, 200, 130]),
                    TextFragment(text="Professor Smith", bbox=[100, 150, 400, 200]),
                ],
                visible_text="⟶\nProfessor Smith",
                accessible_text="Professor Smith",
                bbox=[100, 100, 400, 200],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[PagePlan(page_number=1, elements=[caption])],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                [fragment.text for fragment in caption.visible_fragments],
                ["Professor Smith"],
            )
            self.assertEqual(
                plan.pages[0].artifacts[0].reason, ArtifactReason.DECORATION
            )

    def test_transformations_have_exact_reconstructable_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            dehyphenated = PageElement(
                role=ElementRole.P,
                visible_text="mathe-\nmatics",
                accessible_text="mathematics",
                bbox=[100, 100, 900, 200],
                review_status=ReviewStatus.MODEL_REVIEWED,
            )
            unverified = PageElement(
                role=ElementRole.P,
                visible_text="sucess",
                accessible_text="success",
                bbox=[100, 300, 900, 400],
                review_status=ReviewStatus.MODEL_REVIEWED,
            )
            date_range = PageElement(
                role=ElementRole.P,
                visible_text="September 15–17, 2006.",
                accessible_text="September 15–17, 2006.",
                bbox=[100, 500, 900, 600],
                review_status=ReviewStatus.MODEL_REVIEWED,
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[
                    PagePlan(
                        page_number=1,
                        coordinate_space=CoordinateSpace.NORMALIZED,
                        elements=[dehyphenated, unverified, date_range],
                    )
                ],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                dehyphenated.transformations[0].kind,
                TransformationKind.LINE_BREAK_DEHYPHENATION,
            )
            self.assertEqual(unverified.transformations[0].kind, TransformationKind.UNVERIFIED)
            self.assertEqual(date_range.visible_text, "September 15–17, 2006.")
            self.assertEqual(date_range.accessible_text, "September 15 to 17, 2006.")
            self.assertEqual(
                date_range.transformations[0].kind,
                TransformationKind.DATE_RANGE_EXPANSION,
            )
            self.assertEqual(transformation_errors(plan), [])


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
        model_page = ModelPagePlan(page_number=99, elements=[model_element])

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
        self.assertEqual(proposal.page_number, 1)
        self.assertEqual(proposal.elements[0].id, "p0001-e0001")
        self.assertEqual(decision.canonical_page.page_number, 1)
        self.assertEqual(decision.canonical_page.elements[0].id, "p0001-e0001")
        review_content = responses.calls[1]["input"][1]["content"]
        self.assertEqual(review_content[0]["type"], "input_image")
        self.assertTrue(review_content[1]["text"].startswith("EVIDENCE FIRST:"))
        self.assertTrue(review_content[2]["text"].startswith("DETERMINISTIC DIAGNOSTICS:"))
        self.assertTrue(review_content[3]["text"].startswith("FIRST-MODEL PROPOSAL:"))
        self.assertNotIn('"tokens"', review_content[3]["text"])


if __name__ == "__main__":
    unittest.main()
