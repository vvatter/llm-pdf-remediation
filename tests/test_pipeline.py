from __future__ import annotations

import tempfile
import unittest
from unittest import mock
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pikepdf
import pymupdf
from pydantic import ValidationError

from pdf_accessibility.compiler import (
    AnchorFont,
    LinePlacement,
    RegionLineCandidate,
    _align_element_fragments,
    _collision_safe_region_lines,
    _page_anchor_chunks,
    _repair_missing_cid_to_gid_maps,
    compile_tagged_pdf,
)
from pdf_accessibility.models import (
    ArtifactReason,
    CoordinateSpace,
    DocumentPlan,
    ElementRole,
    FindingCategory,
    FormWidgetPlan,
    PageElement,
    PageFlow,
    PagePlan,
    RemediationMode,
    ReviewFinding,
    ReviewSeverity,
    ReviewStatus,
    TextFragment,
    TableHeaderScope,
    TextTransformation,
    TransformationKind,
    exact_text_tokens,
)
from pdf_accessibility.plans import load_document_plan
from pdf_accessibility.forms import (
    form_snapshot,
    set_field_tooltip,
    terminal_field_dictionary,
)
from pdf_accessibility.preflight import (
    SourceMetadata,
    inspect_pdf,
    read_source_metadata,
    run_verapdf,
)
from pdf_accessibility.evidence import diagnostics_for, evidence_from_packet
from pdf_accessibility.extract import PagePacket
from pdf_accessibility.planner import (
    HEADING_GUIDANCE,
    RUNNING_MATTER_GUIDANCE,
    TABLE_GUIDANCE,
    PLANNER_PROMPT_VERSION,
    PROPOSAL_SYSTEM_PROMPT,
    REVIEW_PROMPT_VERSION,
    REVIEW_SYSTEM_PROMPT,
    _validated_page_plan,
    ModelPageElement,
    ModelPagePlan,
    ModelReviewDecision,
    infer_title,
    normalize_document_pages,
    propose_page,
    review_page,
)
from pdf_accessibility.title_history import load_recent_titles, remember_title
from pdf_accessibility.refine import (
    _complete_rectangular_table_grids,
    _normalize_static_form_markers,
    canonicalize_transformations,
    refine_document_plan,
    transformation_errors,
)
from pdf_accessibility.validate import (
    REMEDIATION_PRODUCER,
    REMEDIATION_SUMMARY,
    REMEDIATION_TOOL,
    add_pdfua_declaration,
    _fidelity_within_tolerance,
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
    def test_heading_guidance_requires_visible_semantic_hierarchy(self) -> None:
        self.assertEqual(PLANNER_PROMPT_VERSION, "proposal-v18")
        self.assertEqual(REVIEW_PROMPT_VERSION, "review-v18")
        for prompt in (PROPOSAL_SYSTEM_PROMPT, REVIEW_SYSTEM_PROMPT):
            self.assertIn(HEADING_GUIDANCE, prompt)
            self.assertIn(RUNNING_MATTER_GUIDANCE, prompt)
            self.assertIn(TABLE_GUIDANCE, prompt)
            normalized = " ".join(prompt.split())
            self.assertIn("heading must be visible text", normalized)
            self.assertIn("short key-value or metadata label", normalized)
            self.assertIn("H3 only for a subsection genuinely nested", normalized)
            self.assertIn("keep the content as P rather than synthesizing one", normalized)
            self.assertIn("genuine two-dimensional data table", normalized)
            self.assertIn("repeated roster, schedule, comparison matrix", normalized)
            self.assertIn("cells consecutively in row-major order", normalized)
            self.assertIn("each logical row spans the same number of columns", normalized)
            self.assertIn("printed title is generic", normalized)
            self.assertIn("exactly one form_widgets entry", normalized)
            self.assertIn("terminal field's /TU tooltip", normalized)
            self.assertIn("Do not return an instruction-only label", normalized)
            self.assertIn("generic internal identifiers", normalized)

    def test_search_title_candidate_is_separate_from_printed_title(self) -> None:
        page = PagePlan(
            page_number=1,
            document_title_candidate="Spring Celebration, UF Mathematics, 2024",
            elements=[element(ElementRole.DOCUMENT_TITLE, "Spring Celebration")],
        )

        self.assertEqual(
            infer_title(Path("spring-celebration-2024.pdf"), [page]),
            "Spring Celebration, UF Mathematics, 2024",
        )
        self.assertEqual(page.elements[0].accessible_text, "Spring Celebration")

    def test_incomplete_proposal_table_is_left_for_independent_review(self) -> None:
        page = _validated_page_plan(
            {
                "coordinate_space": "normalized_0_1000",
                "elements": [
                    {
                        "role": "TH",
                        "visible_text": "Printed Names",
                        "accessible_text": "Printed Names",
                        "table_id": "roster",
                        "bbox": [100, 100, 400, 150],
                    },
                    {
                        "role": "TD",
                        "visible_text": "",
                        "accessible_text": "",
                        "table_id": "roster",
                        "table_row": 1,
                        "bbox": [100, 160, 400, 220],
                    },
                ],
            },
            1,
            recover_incomplete_tables=True,
        )

        self.assertEqual([element.role for element in page.elements], [ElementRole.P])
        self.assertEqual(page.elements[0].visible_text, "Printed Names")
        self.assertTrue(
            any(
                finding.category == FindingCategory.SEMANTIC_ROLE
                and finding.severity == ReviewSeverity.WARNING
                for finding in page.findings
            )
        )

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

        normalized = normalize_document_pages(Path("document.pdf"), pages)

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

        normalized = normalize_document_pages(Path("document.pdf"), pages)

        self.assertEqual(
            normalized[0].elements[0].text,
            "INSIDE THIS ISSUE Notes from the Chair 1 Faculty Notes 2",
        )


class CompileTests(unittest.TestCase):
    def test_missing_embedded_cid_to_gid_map_becomes_explicit_identity(self) -> None:
        with pikepdf.Pdf.new() as pdf:
            descriptor = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/FontDescriptor"),
                    FontFile2=pdf.make_stream(b"embedded font program"),
                )
            )
            embedded = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/CIDFontType2"),
                    FontDescriptor=descriptor,
                )
            )
            unembedded = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Font"),
                    Subtype=pikepdf.Name("/CIDFontType2"),
                    FontDescriptor=pdf.make_indirect(
                        pikepdf.Dictionary(Type=pikepdf.Name("/FontDescriptor"))
                    ),
                )
            )

            _repair_missing_cid_to_gid_maps(pdf)

            self.assertEqual(str(embedded.CIDToGIDMap), "/Identity")
            self.assertNotIn("/CIDToGIDMap", unembedded)

    def test_source_fidelity_allows_low_resolution_raster_antialiasing_only(self) -> None:
        samples = {
            "72": {
                "maximum_page_mean_absolute_channel_difference": 0.0567,
                "maximum_page_sample_fraction_over_16": 0.2083,
            },
            "150": {
                "maximum_page_mean_absolute_channel_difference": 0.0371,
                "maximum_page_sample_fraction_over_16": 0.1308,
            },
        }

        self.assertTrue(_fidelity_within_tolerance(samples, True))
        samples["150"]["maximum_page_mean_absolute_channel_difference"] = 0.0567
        self.assertFalse(_fidelity_within_tolerance(samples, True))

    def test_consecutive_list_items_compile_as_a_real_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Recipients")
            page.insert_text((72, 100), "Ada Lovelace")
            page.insert_text((72, 120), "Emmy Noether")
            page.insert_text((72, 150), "Presented by the department")
            document.save(source)
            document.close()
            plan = DocumentPlan(
                source_file=source.name,
                title="Recipients",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.H1, "Recipients", top=80),
                            element(ElementRole.LI, "Ada Lovelace", top=140),
                            element(ElementRole.LI, "Emmy Noether", top=200),
                            element(ElementRole.P, "Presented by the department", top=260),
                        ],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan)

            with pikepdf.Pdf.open(output) as pdf:
                children = list(pdf.Root.StructTreeRoot.K.K)
                self.assertEqual(
                    [str(child.S) for child in children], ["/H1", "/L", "/P"]
                )
                self.assertEqual(str(children[1].A.O), "/List")
                self.assertEqual(str(children[1].A.ListNumbering), "/None")
                list_items = list(children[1].K)
                self.assertEqual([str(item.S) for item in list_items], ["/LI", "/LI"])
                self.assertTrue(
                    all(str(item.K[0].S) == "/LBody" for item in list_items)
                )

            serialized = serialize_structure_tree(output)
            self.assertEqual(
                [record["role"] for record in serialized["elements"]],
                ["/H1", "/LI", "/LI", "/P"],
            )
            self.assertEqual(serialized["errors"], [])
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])

    def test_genuine_data_table_compiles_with_headers_and_empty_corner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=600, height=400)
            document.save(source)
            document.close()

            def cell(
                role: ElementRole,
                text: str,
                row: int,
                column: int,
                scope: TableHeaderScope | None = None,
                top: float = 200,
            ) -> PageElement:
                return PageElement(
                    role=role,
                    visible_text=text,
                    accessible_text=text,
                    table_id="performance-rubric",
                    table_row=row,
                    table_column=column,
                    header_scope=scope,
                    bbox=[100 + 250 * column, top, 330 + 250 * column, top + 80],
                )

            plan = DocumentPlan(
                source_file=source.name,
                title="Performance Rubric",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.H1, "Performance rubric", top=60),
                            cell(ElementRole.TH, "January", 0, 1, TableHeaderScope.COLUMN),
                            cell(ElementRole.TH, "May", 0, 2, TableHeaderScope.COLUMN),
                            cell(
                                ElementRole.TH,
                                "Meets expectations",
                                1,
                                0,
                                TableHeaderScope.ROW,
                                top=400,
                            ),
                            cell(ElementRole.TD, "Pass one exam", 1, 1, top=400),
                            cell(ElementRole.TD, "Pass two exams", 1, 2, top=400),
                            cell(
                                ElementRole.TH,
                                "Below expectations",
                                2,
                                0,
                                TableHeaderScope.ROW,
                                top=600,
                            ),
                            cell(ElementRole.TD, "", 2, 1, top=600),
                            cell(ElementRole.TD, "No pass", 2, 2, top=600),
                        ],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan)

            with pikepdf.Pdf.open(output) as pdf:
                children = list(pdf.Root.StructTreeRoot.K.K)
                self.assertEqual([str(child.S) for child in children], ["/H1", "/Table"])
                rows = list(children[1].K)
                self.assertEqual([str(row.S) for row in rows], ["/TR", "/TR", "/TR"])
                self.assertEqual(
                    [str(item.S) for item in rows[0].K], ["/TD", "/TH", "/TH"]
                )
                self.assertEqual(len(rows[0].K[0].K), 0)
                january_table_attributes = next(
                    item for item in rows[0].K[1].A if str(item.O) == "/Table"
                )
                self.assertEqual(str(january_table_attributes.Scope), "/Column")
                self.assertEqual(len(rows[2].K[1].K), 0)
                self.assertTrue(
                    any(str(item.O) == "/Layout" for item in rows[2].K[1].A)
                )

            serialized = serialize_structure_tree(output)
            self.assertEqual(serialized["errors"], [])
            self.assertEqual(
                [record["role"] for record in serialized["elements"]],
                ["/H1", "/TH", "/TH", "/TH", "/TD", "/TD", "/TH", "/TD", "/TD"],
            )
            self.assertEqual(
                [record["table_column"] for record in serialized["elements"][1:]],
                [1, 2, 0, 1, 2, 0, 1, 2],
            )
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])
            candidate = Path(temp) / "candidate.pdf"
            add_pdfua_declaration(output, candidate, plan)
            vera_ok, vera_report = run_verapdf(candidate)
            if vera_ok is not None:
                self.assertTrue(vera_ok, vera_report)

    def test_table_row_spans_do_not_create_duplicate_placeholder_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=600, height=400)
            document.save(source)
            document.close()

            def cell(
                role: ElementRole,
                text: str,
                row: int,
                column: int,
                *,
                scope: TableHeaderScope | None = None,
                row_span: int = 1,
            ) -> PageElement:
                return PageElement(
                    role=role,
                    visible_text=text,
                    accessible_text=text,
                    table_id="exam-attempts",
                    table_row=row,
                    table_column=column,
                    table_row_span=row_span,
                    header_scope=scope,
                    bbox=[100 + 250 * column, 100 + 150 * row, 330 + 250 * column, 220 + 150 * row],
                )

            plan = DocumentPlan(
                source_file=source.name,
                title="Exam Attempts",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.H1, "Exam attempts", top=40),
                            cell(
                                ElementRole.TH,
                                "Attempt",
                                0,
                                0,
                                scope=TableHeaderScope.ROW,
                                row_span=3,
                            ),
                            cell(
                                ElementRole.TH,
                                "January",
                                0,
                                1,
                                scope=TableHeaderScope.COLUMN,
                            ),
                            cell(
                                ElementRole.TH,
                                "May",
                                0,
                                2,
                                scope=TableHeaderScope.COLUMN,
                            ),
                            cell(ElementRole.TD, "Pass", 1, 1),
                            cell(ElementRole.TD, "Fail", 1, 2),
                            cell(ElementRole.TD, "", 2, 1),
                            cell(ElementRole.TD, "", 2, 2),
                        ],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan)

            with pikepdf.Pdf.open(output) as pdf:
                table = list(pdf.Root.StructTreeRoot.K.K)[1]
                rows = list(table.K)
                self.assertEqual([len(row.K) for row in rows], [3, 2, 2])
                attributes = next(
                    item for item in rows[0].K[0].A if str(item.O) == "/Table"
                )
                self.assertEqual(int(attributes.RowSpan), 3)

            serialized = serialize_structure_tree(output)
            self.assertEqual(serialized["errors"], [])
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])
            self.assertEqual(
                [record["table_column"] for record in serialized["elements"][1:]],
                [0, 1, 2, 1, 2, 1, 2],
            )
            candidate = Path(temp) / "candidate.pdf"
            add_pdfua_declaration(output, candidate, plan)
            vera_ok, vera_report = run_verapdf(candidate)
            if vera_ok is not None:
                self.assertTrue(vera_ok, vera_report)

    def test_preflight_snapshots_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw.pdf"
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(raw)
            document.close()
            with pikepdf.Pdf.open(raw) as pdf:
                pdf.docinfo[pikepdf.Name.Author] = "Ada Lovelace"
                pdf.docinfo[pikepdf.Name.Subject] = "Analytical Engine"
                pdf.docinfo[pikepdf.Name.Keywords] = "mathematics; history"
                pdf.docinfo[pikepdf.Name.Creator] = "TeX"
                pdf.docinfo[pikepdf.Name.Producer] = "Ghostscript 5.50"
                pdf.docinfo[pikepdf.Name.CreationDate] = "D:20000102030405"
                with pdf.open_metadata(
                    set_pikepdf_as_editor=False, update_docinfo=False
                ) as metadata:
                    metadata["dc:creator"] = ["Ada Lovelace"]
                    metadata["dc:description"] = "A history of the Analytical Engine."
                    metadata["pdf:Keywords"] = "mathematics; history"
                    metadata["xmp:CreatorTool"] = "TeX"
                    metadata["pdf:Producer"] = "Ghostscript 5.50"
                    metadata["xmp:CreateDate"] = "2000-01-02T03:04:05"
                pdf.save(source)

            snapshot = read_source_metadata(source)

            self.assertEqual(snapshot.author, "Ada Lovelace")
            self.assertEqual(snapshot.subject, "Analytical Engine")
            self.assertEqual(snapshot.keywords, "mathematics; history")
            self.assertEqual(snapshot.xmp_authors, ["Ada Lovelace"])
            self.assertEqual(
                snapshot.description, "A history of the Analytical Engine."
            )
            self.assertEqual(snapshot.xmp_keywords, "mathematics; history")
            self.assertEqual(snapshot.creation_date, "D:20000102030405")
            self.assertEqual(snapshot.xmp_creation_date, "2000-01-02T03:04:05")
            self.assertEqual(
                snapshot.encoding_software, ["TeX", "Ghostscript 5.50"]
            )

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
            page.insert_text((72, 72), "Visible archival text")
            page.insert_link(
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(72, 55, 190, 78),
                    "uri": "https://example.edu/archive",
                }
            )
            document.save(source)
            document.close()

            plan = DocumentPlan(
                source_file=source.name,
                title="Test Document",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.DOCUMENT_TITLE, "Test Document"),
                            element(
                                ElementRole.P,
                                " ".join(["Visible archival text"] * 20),
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
            self.assertTrue(report["extraction_compatible"])
            self.assertTrue(report["transformations_valid"])
            self.assertTrue(report["source_visual_fidelity_ok"])
            extracted = subprocess.run(
                ["pdftotext", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("Visible archival text", extracted)
            self.assertEqual(extracted.count("Visible archival text"), 20)
            self.assertNotIn("Historical diagram", extracted)
            with pikepdf.Pdf.open(output) as pdf:
                self.assertTrue(pdf.Root.MarkInfo.Marked)
                self.assertEqual(str(pdf.Root.Lang), "en-US")
                self.assertEqual(str(pdf.pages[0].obj.Tabs), "/S")
                self.assertIn("/PageLabels", pdf.Root)
                annotation = pdf.pages[0].Annots[0]
                self.assertEqual(
                    str(annotation.Contents),
                    "Link to https://example.edu/archive",
                )
                self.assertGreaterEqual(int(annotation.StructParent), 1)
                structure_roles = [
                    str(item.S) for item in pdf.Root.StructTreeRoot.K.K
                ]
                self.assertIn("/Link", structure_roles)
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
                    pdf.pages[0].Contents[0].read_bytes(),
                    b"q\n/Artifact BMC\n/Span <</ActualText <FEFF0020>>> BDC\n",
                )
                self.assertEqual(
                    pdf.pages[0].Contents[-4].read_bytes(), b"EMC\nEMC\nQ\n"
                )

    def test_fillable_widgets_are_preserved_tagged_described_and_editable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            filled = Path(temp) / "filled.pdf"
            document = pymupdf.open()
            page = document.new_page(width=400, height=240)
            text_widget = pymupdf.Widget()
            text_widget.field_name = "Text3"
            text_widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            text_widget.rect = pymupdf.Rect(120, 50, 300, 75)
            page.add_widget(text_widget)
            checkbox = pymupdf.Widget()
            checkbox.field_name = "Approved"
            checkbox.field_label = "Approve this request"
            checkbox.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            checkbox.rect = pymupdf.Rect(120, 95, 140, 115)
            page.add_widget(checkbox)
            document.save(source)
            document.close()
            heading = PageElement(
                role=ElementRole.H1,
                visible_text="Sample form",
                accessible_text="Sample form",
                bbox=[100, 50, 900, 180],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample Form",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[heading],
                        form_widgets=[
                            FormWidgetPlan(widget_index=0, description="Student name"),
                            FormWidgetPlan(
                                widget_index=1,
                                description="Approve this request",
                            ),
                        ],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan)

            source_snapshot = form_snapshot(source)
            output_snapshot = form_snapshot(output)
            self.assertEqual(
                [widget["name"] for widget in source_snapshot["widgets"]],
                [widget["name"] for widget in output_snapshot["widgets"]],
            )
            self.assertEqual(source_snapshot["widgets_missing_tooltips"], 1)
            self.assertEqual(source_snapshot["widgets_with_generic_descriptions"], 0)
            self.assertEqual(output_snapshot["widgets_missing_tooltips"], 0)
            self.assertEqual(output_snapshot["widgets_with_generic_descriptions"], 0)
            self.assertEqual(output_snapshot["accessibility_errors"], [])
            preflight = inspect_pdf(source, RemediationMode.AUTO)
            self.assertEqual(preflight.widget_count, 2)
            self.assertGreaterEqual(preflight.form_field_count, 2)
            self.assertEqual(preflight.widgets_missing_descriptions, 1)
            self.assertEqual(preflight.widgets_missing_tooltips, 1)
            self.assertEqual(preflight.widgets_with_generic_descriptions, 0)
            with pikepdf.Pdf.open(output) as pdf:
                widgets = [
                    annotation
                    for annotation in pdf.pages[0].Annots
                    if str(annotation.Subtype) == "/Widget"
                ]
                self.assertEqual(len(widgets), 2)
                self.assertTrue(all("/StructParent" in widget for widget in widgets))
                self.assertEqual(
                    [str(widget.get("/TU", "")) for widget in widgets],
                    ["Student name", "Approve this request"],
                )
                roles = [str(item.S) for item in pdf.Root.StructTreeRoot.K.K]
                self.assertEqual(roles, ["/H1", "/Form", "/Form"])
                form_elements = list(pdf.Root.StructTreeRoot.K.K)[1:]
                self.assertEqual(
                    [str(item.Alt) for item in form_elements],
                    ["Student name", "Approve this request"],
                )
                parent_numbers = list(pdf.Root.StructTreeRoot.ParentTree.Nums)
                parent_keys = [
                    int(parent_numbers[index])
                    for index in range(0, len(parent_numbers), 2)
                ]
                self.assertEqual(parent_keys, sorted(parent_keys))
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )
            report = validate_output(
                source, output, plan, reference_source=source
            )
            self.assertTrue(report["form_fields_preserved"])
            self.assertTrue(report["form_descriptions_match_plan"])
            self.assertTrue(report["form_accessibility_policy_ok"])

            with pymupdf.open(output) as document:
                page = document[0]
                widgets = list(page.widgets())
                field = next(
                    widget
                    for widget in widgets
                    if widget.field_name == "Text3"
                )
                field.field_value = "Ada Lovelace"
                field.update()
                document.save(filled)
            with pymupdf.open(filled) as document:
                page = document[0]
                values = {
                    widget.field_name: widget.field_value
                    for widget in page.widgets()
                }
            self.assertEqual(values["Text3"], "Ada Lovelace")

    def test_field_tooltip_is_written_to_terminal_field_owner(self) -> None:
        pdf = pikepdf.Pdf.new()
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Widget"),
            )
        )
        field = pdf.make_indirect(
            pikepdf.Dictionary(
                FT=pikepdf.Name("/Tx"),
                T=pikepdf.String("Student name"),
                Kids=pikepdf.Array([widget]),
            )
        )
        widget[pikepdf.Name("/Parent")] = field

        self.assertEqual(terminal_field_dictionary(widget).objgen, field.objgen)
        set_field_tooltip(widget, "Student name")
        self.assertEqual(str(field.get("/TU", "")), "Student name")
        self.assertNotIn("/TU", widget)

        group = pdf.make_indirect(
            pikepdf.Dictionary(T=pikepdf.String("Committee"))
        )
        merged_widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Widget"),
                FT=pikepdf.Name("/Tx"),
                T=pikepdf.String("Chair"),
                Parent=group,
            )
        )
        self.assertEqual(
            terminal_field_dictionary(merged_widget).objgen,
            merged_widget.objgen,
        )

    def test_pdfua_form_with_two_missing_tooltips_is_not_passed_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            compiled = Path(temp) / "compiled.pdf"
            damaged = Path(temp) / "missing-tooltips.pdf"
            document = pymupdf.open()
            page = document.new_page(width=400, height=240)
            for index, name in enumerate(
                ("Co-Chair or Member 1", "Graduate Coordinator Approval")
            ):
                widget = pymupdf.Widget()
                widget.field_name = name
                widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                widget.rect = pymupdf.Rect(80, 60 + index * 50, 300, 85 + index * 50)
                page.add_widget(widget)
            document.save(source)
            document.close()
            plan = DocumentPlan(
                source_file=source.name,
                title="Supervisory Committee",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            PageElement(
                                role=ElementRole.H1,
                                visible_text="Supervisory Committee",
                                accessible_text="Supervisory Committee",
                                bbox=[100, 50, 900, 180],
                            )
                        ],
                        form_widgets=[
                            FormWidgetPlan(
                                widget_index=0,
                                description="Co-Chair or Committee Member 1 name",
                            ),
                            FormWidgetPlan(
                                widget_index=1,
                                description="Graduate Coordinator approval",
                            ),
                        ],
                    )
                ],
            )
            compile_tagged_pdf(source, compiled, plan)
            with pikepdf.Pdf.open(compiled) as pdf:
                widgets = [
                    annotation
                    for annotation in pdf.pages[0].Annots
                    if str(annotation.get("/Subtype", "")) == "/Widget"
                ]
                for widget in widgets:
                    owner = terminal_field_dictionary(widget)
                    del owner["/TU"]
                pdf.save(damaged)

            snapshot = form_snapshot(damaged)
            self.assertEqual(snapshot["widget_count"], 2)
            self.assertEqual(snapshot["widgets_missing_tooltips"], 2)
            self.assertEqual(
                [
                    widget["accessible_name_source"]
                    for widget in snapshot["widgets"]
                ],
                ["field_name_fallback", "field_name_fallback"],
            )
            with mock.patch(
                "pdf_accessibility.preflight.run_verapdf",
                return_value=(True, {}),
            ):
                preflight = inspect_pdf(damaged, RemediationMode.AUTO)
            self.assertTrue(preflight.pdfua_valid)
            self.assertNotEqual(
                preflight.selected_mode,
                RemediationMode.PASS_THROUGH,
            )
            self.assertEqual(preflight.widgets_missing_tooltips, 2)
            self.assertTrue(preflight.form_accessibility_errors)

    def test_anchor_font_preserves_circle_glyphs_in_extracted_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "Full and open circles")
            document.save(source)
            document.close()
            plan = DocumentPlan(
                source_file=source.name,
                title="Circle glyphs",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(
                                ElementRole.P,
                                "● ○ Full and open circles",
                            )
                        ],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan)
            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

            self.assertIn("● ○ Full and open circles", extracted)

    def test_inline_formula_uses_spoken_actual_text_and_structural_alt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=700, height=300)
            document.save(source)
            document.close()
            visible = (
                "Abstract: Given a permutation p = p₁p₂...pₙ ∈ 𝔖ₙ, a set of "
                "indices i₁ < i₂ < ... < iₖ defines an increasing subsequence if "
                "pᵢ₁ < pᵢ₂ < ... < pᵢₖ."
            )
            accessible = (
                "Abstract: Given a permutation p equals p subscript 1 p subscript 2 "
                "ellipsis p subscript n, an element of the symmetric group S subscript n, "
                "a set of indices i subscript 1 is less than i subscript 2 is less than "
                "ellipsis is less than i subscript k defines an increasing subsequence if "
                "p subscript i subscript 1 is less than p subscript i subscript 2 is less "
                "than ellipsis is less than p subscript i subscript k."
            )
            formulae = [
                (
                    "p = p₁p₂...pₙ ∈ 𝔖ₙ",
                    "p equals p subscript 1 p subscript 2 ellipsis p subscript n, "
                    "an element of the symmetric group S subscript n",
                ),
                (
                    "i₁ < i₂ < ... < iₖ",
                    "i subscript 1 is less than i subscript 2 is less than ellipsis "
                    "is less than i subscript k",
                ),
                (
                    "pᵢ₁ < pᵢ₂ < ... < pᵢₖ",
                    "p subscript i subscript 1 is less than p subscript i subscript 2 "
                    "is less than ellipsis is less than p subscript i subscript k",
                ),
            ]
            paragraph = PageElement(
                role=ElementRole.P,
                visible_text=visible,
                accessible_text=accessible,
                transformations=[
                    TextTransformation(
                        kind=TransformationKind.FORMULA_SPOKEN_EQUIVALENT,
                        source_text=notation,
                        replacement_text=spoken,
                        rationale="Reviewed spoken mathematics.",
                    )
                    for notation, spoken in formulae
                ],
                bbox=[50, 50, 950, 500],
            )
            canonicalize_transformations(paragraph)
            plan = DocumentPlan(
                source_file=source.name,
                title="Permutation abstract",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            extraction_text = visible.replace("𝔖", "S")
            extracted_formulae = [
                formulae[0][0].replace("𝔖", "S"),
                formulae[1][0],
                formulae[2][0],
            ]
            self.assertEqual(paragraph.extraction_text, extraction_text)
            self.assertEqual(
                [item.text for item in paragraph.formula_spans],
                extracted_formulae,
            )
            self.assertEqual(transformation_errors(plan), [])

            compile_tagged_pdf(source, output, plan)
            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("p subscript 1", extracted)
            self.assertIn("symmetric group S subscript n", extracted)
            self.assertNotIn("p₁p₂", extracted)
            self.assertEqual(extracted.strip(), accessible)

            serialized = serialize_structure_tree(output)
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])
            formula_blocks = [
                block
                for block in serialized["elements"][0]["blocks"]
                if block["role"] == "/Formula"
            ]
            self.assertEqual(
                [block["text"] for block in formula_blocks],
                [item[1] for item in formulae],
            )
            self.assertEqual(
                [block["alt_text"] for block in formula_blocks],
                [item[1] for item in formulae],
            )
            self.assertEqual(
                [block["actual_text"] for block in formula_blocks],
                [item[1] for item in formulae],
            )

    def test_stacked_fraction_uses_reviewed_speech_in_plain_text_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=300, height=150)
            document.save(source)
            document.close()

            visible = "The fraction ²⁄₁₁."
            accessible = "The fraction two elevenths."
            paragraph = PageElement(
                role=ElementRole.P,
                visible_text=visible,
                accessible_text=accessible,
                transformations=[
                    TextTransformation(
                        kind=TransformationKind.FORMULA_SPOKEN_EQUIVALENT,
                        source_text="²⁄₁₁",
                        replacement_text="two elevenths",
                        rationale="Reviewed spoken fraction.",
                    )
                ],
                bbox=[100, 100, 900, 300],
            )
            canonicalize_transformations(paragraph)
            plan = DocumentPlan(
                source_file=source.name,
                title="Fraction",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan)

            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(extracted.strip(), accessible)
            serialized = serialize_structure_tree(output)
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])
            formula = serialized["elements"][0]["blocks"][0]
            self.assertEqual(formula["text"], "two elevenths")
            self.assertEqual(formula["alt_text"], "two elevenths")
            self.assertEqual(formula["actual_text"], "two elevenths")
            with pikepdf.Pdf.open(output) as pdf:
                formula_stream = pdf.pages[0].Contents[-1].read_bytes()
            encoded_speech = "two elevenths".encode("utf-16-be").hex().upper().encode()
            self.assertIn(encoded_speech, formula_stream)
            self.assertNotIn(b"/ActualText", formula_stream)

    def test_structure_comparison_uses_emitted_nonempty_regions(self) -> None:
        paragraph = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(text="“", bbox=[0, 0, 20, 20]),
                TextFragment(text="First ", bbox=[100, 100, 400, 120]),
                TextFragment(text="second", bbox=[100, 121, 400, 140]),
                TextFragment(text="”", bbox=[900, 900, 920, 920]),
            ],
            visible_text="“First second”",
            accessible_text="“First second”",
            bbox=[0, 0, 920, 920],
        )
        plan = DocumentPlan(
            source_file="source.pdf",
            title="Quoted paragraph",
            pages=[PagePlan(page_number=1, elements=[paragraph])],
        )
        serialized = {
            "errors": [],
            "elements": [
                {"role": "/P", "text": "“First ", "alt_text": ""},
                {"role": "/P", "text": "second”", "alt_text": ""},
            ],
        }

        self.assertEqual(compare_structure_to_plan(serialized, plan), [])

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
                self.assertEqual(anchor_stream.count(b" Tj\n"), 1)
                self.assertNotIn(b"/ActualText", anchor_stream)
                self.assertEqual(anchor_stream.count(b" Tm "), 1)
            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(
                extracted.strip(),
                "First semantic line Second semantic line",
            )
            with pymupdf.open(output) as document:
                self.assertEqual(
                    [word[4] for word in document[0].get_text("words", sort=False)],
                    ["First", "semantic", "line", "Second", "semantic", "line"],
                )
                self.assertEqual(
                    len(
                        {
                            (word[5], word[6])
                            for word in document[0].get_text("words", sort=False)
                        }
                    ),
                    1,
                )
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )

    def test_dense_paragraph_uses_collision_free_page_width_without_hard_wraps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()

            visual_lines = [
                "Persi Diaconis studied violin at Juilliard and magic with Dai Vernon, who has been called the greatest",
                "magician in the US. Then he took a degree in mathematics at College of the City of New York and",
                "doctorate in statistics at Harvard. He is Mary Sunseri Professor of Statistics and Professor of",
                "Mathematics at Stanford University. He has held visiting positions at AT&T Bell Labs, Harvard, MIT, and",
                "Cornell. He was a recipient of a MacArthur Grant. For more information, see the condensed version of",
                "the entry from Mathematical People, edited by Albers and Alexanderson, 1985.",
            ]
            exact = " ".join(visual_lines)
            document = pymupdf.open()
            page = document.new_page(width=612, height=792)
            for index, text in enumerate(visual_lines):
                page.insert_text((182, 172 + index * 7), text, fontsize=4.25)
            document.save(geometry)
            document.close()

            paragraph = PageElement(
                role=ElementRole.P,
                visible_text=exact,
                accessible_text=exact,
                bbox=[295, 205, 680, 275],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Dense paragraph",
                pages=[PagePlan(page_number=1, elements=[paragraph])],
            )

            compile_tagged_pdf(source, output, plan, geometry_source=geometry)

            with pikepdf.Pdf.open(output) as pdf:
                anchor_stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertEqual(anchor_stream.count(b" Tj\n"), 1)
                self.assertNotIn(b"/ActualText", anchor_stream)
                paragraph_element = pdf.Root.StructTreeRoot.K.K[0]
                self.assertNotIn("/ActualText", paragraph_element)
            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(extracted.strip(), exact)
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )

    def test_logical_lines_preserve_semantic_breaks_not_visual_wraps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=400, height=250)
            document.save(source)
            document.close()
            document = pymupdf.open()
            page = document.new_page(width=400, height=250)
            for y, text in enumerate(
                [
                    "2.3 Example Author",
                    "Title: A long title that",
                    "wraps in the source",
                    "Abstract: First sentence continues",
                    "without a semantic break.",
                ],
                start=1,
            ):
                page.insert_text((30, y * 35), text)
            document.save(geometry)
            document.close()
            exact = (
                "2.3 Example Author\n"
                "Title: A long title that wraps in the source\n"
                "Abstract: First sentence continues without a semantic break."
            )
            item = PageElement(
                role=ElementRole.LI,
                visible_text=exact,
                accessible_text=exact,
                bbox=[50, 50, 950, 900],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Logical lines",
                pages=[PagePlan(page_number=1, elements=[item])],
            )

            compile_tagged_pdf(source, output, plan, geometry_source=geometry)

            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(extracted.strip(), exact)
            with pikepdf.Pdf.open(output) as pdf:
                stream = pdf.pages[0].Contents[-1].read_bytes()
                self.assertEqual(stream.count(b" Tj\n"), 3)
                self.assertEqual(stream.count(b" Tm "), 3)
            self.assertEqual(
                compare_structure_to_plan(serialize_structure_tree(output), plan), []
            )

    def test_interleaved_list_items_use_nonoverlapping_fragment_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=400, height=200)
            document.save(source)
            document.close()

            introduction = "Two additional items:"
            first_top = "(i) Friday in Hall A:"
            first_bottom = "First topic"
            second = "(ii) Saturday in Hall B: Second topic"
            document = pymupdf.open()
            page = document.new_page(width=400, height=200)
            page.insert_text((30, 70), introduction)
            page.insert_text((160, 70), first_top)
            page.insert_text((30, 95), first_bottom)
            page.insert_text((120, 95), second)
            document.save(geometry)
            document.close()

            introduction_element = PageElement(
                role=ElementRole.P,
                visible_fragments=[
                    TextFragment(text=introduction, bbox=[75, 280, 385, 370])
                ],
                visible_text=introduction,
                accessible_text=introduction,
                bbox=[75, 280, 385, 370],
            )
            first_item = PageElement(
                role=ElementRole.LI,
                visible_fragments=[
                    TextFragment(text=first_top, bbox=[400, 280, 690, 370]),
                    TextFragment(text=first_bottom, bbox=[75, 405, 250, 495]),
                ],
                visible_text=f"{first_top} {first_bottom}",
                accessible_text=f"{first_top} {first_bottom}",
                bbox=[75, 280, 690, 495],
            )
            second_item = PageElement(
                role=ElementRole.LI,
                visible_fragments=[
                    TextFragment(text=second, bbox=[300, 405, 845, 495])
                ],
                visible_text=second,
                accessible_text=second,
                bbox=[300, 405, 845, 495],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Interleaved items",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[introduction_element, first_item, second_item],
                    )
                ],
            )

            compile_tagged_pdf(source, output, plan, geometry_source=geometry)

            with pikepdf.Pdf.open(output) as pdf:
                streams = [item.read_bytes() for item in pdf.pages[0].Contents]
                list_streams = [stream for stream in streams if b"/LBody" in stream]
                self.assertEqual(len(list_streams), 2)
                self.assertEqual(list_streams[0].count(b" Tj\n"), 2)
                self.assertEqual(list_streams[1].count(b" Tj\n"), 1)
                structure_children = list(pdf.Root.StructTreeRoot.K.K)
                list_items = list(structure_children[1].K)
                first_body = list_items[0].K[0]
                self.assertEqual(
                    str(first_body.ActualText),
                    f"{first_top} {first_bottom}",
                )

            serialized = serialize_structure_tree(output)
            self.assertEqual(compare_structure_to_plan(serialized, plan), [])
            extracted = subprocess.run(
                ["pdftotext", "-raw", "-enc", "UTF-8", str(output), "-"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            expected = " ".join(
                [introduction, first_top, first_bottom, second]
            )
            self.assertEqual(" ".join(extracted.split()), expected)

            with pymupdf.open(output) as document:
                words = document[0].get_text("words", sort=True)
                first_word = next(word for word in words if word[4] == "First")
                second_marker = next(word for word in words if word[4] == "(ii)")
                self.assertLess(first_word[2], second_marker[0])

    def test_unresolvable_anchor_overlap_blocks_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            geometry = Path(temp) / "geometry.pdf"
            output = Path(temp) / "output.pdf"
            document = pymupdf.open()
            document.new_page(width=300, height=150)
            document.save(source)
            document.close()
            document = pymupdf.open()
            page = document.new_page(width=300, height=150)
            page.insert_text((30, 60), "Same place")
            document.save(geometry)
            document.close()
            duplicated = [
                PageElement(
                    role=ElementRole.P,
                    visible_text="Same place",
                    accessible_text="Same place",
                    bbox=[100, 300, 400, 500],
                )
                for _ in range(2)
            ]
            plan = DocumentPlan(
                source_file=source.name,
                title="Overlap",
                pages=[PagePlan(page_number=1, elements=duplicated)],
            )

            with self.assertRaisesRegex(
                RuntimeError, "Invisible accessibility text runs still overlap"
            ):
                compile_tagged_pdf(source, output, plan, geometry_source=geometry)

    def test_reviewed_fallback_does_not_create_a_new_neighbor_collision(self) -> None:
        elements = [
            PageElement(role=ElementRole.P, text="First", bbox=[0, 0, 100, 100]),
            PageElement(role=ElementRole.P, text="Second", bbox=[0, 0, 100, 100]),
            PageElement(role=ElementRole.P, text="Third", bbox=[0, 0, 100, 100]),
        ]
        for index, element in enumerate(elements, start=1):
            element.id = f"e{index}"

        def line(text: str, top: float, bottom: float) -> LinePlacement:
            return LinePlacement(
                text=text,
                bbox=(10.0, top, 90.0, bottom),
                chunks=[],
            )

        first = line("First", 0.0, 10.0)
        first_reviewed = line("First", 0.0, 4.0)
        second = line("Second", 5.0, 15.0)
        second_reviewed = line("Second", 4.0, 24.0)
        third = line("Third", 16.0, 26.0)
        candidates = [
            RegionLineCandidate(
                element=elements[0],
                chunks=[],
                fragments=[],
                logical_lines=[first],
                fragment_lines=[first],
                reviewed_fragment_lines=[first_reviewed],
            ),
            RegionLineCandidate(
                element=elements[1],
                chunks=[],
                fragments=[],
                logical_lines=[second],
                fragment_lines=[second],
                reviewed_fragment_lines=[second_reviewed],
            ),
            RegionLineCandidate(
                element=elements[2],
                chunks=[],
                fragments=[],
                logical_lines=[third],
                fragment_lines=[third],
                reviewed_fragment_lines=[third],
            ),
        ]
        font = AnchorFont(
            resource=None,
            supported_codepoints=frozenset(),
            advances={},
        )

        selected, fragment_anchored = _collision_safe_region_lines(
            candidates,
            font,
            page_width=100.0,
        )

        self.assertEqual(selected[0], [first_reviewed])
        self.assertEqual(selected[1], [second])
        self.assertEqual(selected[2], [third])
        self.assertEqual(fragment_anchored, {0})

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

    def test_valid_but_shifted_fragment_box_is_repaired_from_page_text(self) -> None:
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(
                    text="Mark three non-collinear points",
                    bbox=[100, 500, 600, 600],
                )
            ],
            visible_text="Mark three non-collinear points",
            accessible_text="Mark three non-collinear points",
            bbox=[100, 500, 600, 600],
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (20.0, 20.0, 35.0, 30.0, "Mark"),
                (37.0, 20.0, 52.0, 30.0, "three"),
                (54.0, 20.0, 85.0, 30.0, "non-collinear"),
                (87.0, 20.0, 105.0, 30.0, "points"),
                (10.0, 50.0, 25.0, 60.0, "unrelated"),
                (27.0, 50.0, 40.0, 60.0, "line"),
            ],
            width=120,
            height=100,
            geometry_source="native",
        )

        left, top, right, bottom = planned.visible_fragments[0].bbox
        self.assertLess(top, 400)
        self.assertLess(left, right)
        self.assertLess(top, bottom)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)

    def test_half_matching_neighbor_is_repaired_to_exact_phrase(self) -> None:
        planned = PageElement(
            role=ElementRole.H2,
            visible_fragments=[
                TextFragment(text="Excellence Awards", bbox=[100, 450, 700, 600])
            ],
            visible_text="Excellence Awards",
            accessible_text="Excellence Awards",
            bbox=[100, 450, 700, 600],
        )
        chunks = _page_anchor_chunks([planned])[0]

        placements = _align_element_fragments(
            planned,
            chunks,
            [
                (10.0, 20.0, 35.0, 30.0, "Excellence"),
                (37.0, 20.0, 60.0, 30.0, "Awards"),
                (10.0, 50.0, 35.0, 60.0, "Graduate"),
                (37.0, 50.0, 70.0, 60.0, "Excellence"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertLess(planned.visible_fragments[0].bbox[1], 400)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)
        self.assertTrue(
            all(
                placement.bbox[1] < 40
                for items in placements.values()
                for placement in items
            )
        )

    def test_shifted_box_repair_rejects_scattered_common_words(self) -> None:
        original_bbox = [300.0, 50.0, 700.0, 90.0]
        planned = PageElement(
            role=ElementRole.DOCUMENT_TITLE,
            visible_fragments=[
                TextFragment(
                    text="14th Ramanujan Colloquium",
                    bbox=list(original_bbox),
                )
            ],
            visible_text="14th Ramanujan Colloquium",
            accessible_text="14th Ramanujan Colloquium",
            bbox=list(original_bbox),
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (30.0, 5.0, 40.0, 8.0, "14th"),
                (30.0, 50.0, 55.0, 54.0, "Ramanujan"),
                (30.0, 90.0, 60.0, 94.0, "Colloquium"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertEqual(planned.visible_fragments[0].bbox, original_bbox)

    def test_shifted_box_repair_rejects_tokens_split_across_adjacent_lines(self) -> None:
        original_bbox = [100.0, 100.0, 600.0, 180.0]
        planned = PageElement(
            role=ElementRole.LI,
            visible_fragments=[
                TextFragment(
                    text="dim maps R to Z",
                    bbox=list(original_bbox),
                )
            ],
            visible_text="dim maps R to Z",
            accessible_text="dim maps R to Z",
            bbox=list(original_bbox),
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (10.0, 10.0, 18.0, 15.0, "dim"),
                (10.0, 40.0, 22.0, 45.0, "maps"),
                (24.0, 40.0, 28.0, 45.0, "R"),
                (30.0, 40.0, 35.0, 45.0, "to"),
                (37.0, 40.0, 41.0, 45.0, "Z"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertEqual(planned.visible_fragments[0].bbox, original_bbox)

    def test_shifted_box_repair_rejects_partial_repeated_phrase_elsewhere(self) -> None:
        original_bbox = [200.0, 100.0, 800.0, 180.0]
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(
                    text="NINTH ULAM COLLOQUIUM",
                    bbox=list(original_bbox),
                )
            ],
            visible_text="NINTH ULAM COLLOQUIUM",
            accessible_text="NINTH ULAM COLLOQUIUM",
            bbox=list(original_bbox),
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (20.0, 80.0, 35.0, 85.0, "ULAM"),
                (37.0, 80.0, 62.0, 85.0, "COLLOQUIUM"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertEqual(planned.visible_fragments[0].bbox, original_bbox)

    def test_shifted_box_repair_accepts_unique_short_fragment(self) -> None:
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(text="Athens.", bbox=[300, 500, 400, 550])
            ],
            visible_text="Athens.",
            accessible_text="Athens.",
            bbox=[300, 500, 400, 550],
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (20.0, 20.0, 35.0, 30.0, "Athens."),
                (20.0, 70.0, 35.0, 80.0, "unrelated"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertLess(planned.visible_fragments[0].bbox[1], 400)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)

    def test_shifted_box_repair_accepts_uniquely_near_repeated_short_fragment(self) -> None:
        planned = PageElement(
            role=ElementRole.H2,
            visible_fragments=[
                TextFragment(text="Integration Bee", bbox=[100, 400, 800, 520])
            ],
            visible_text="Integration Bee",
            accessible_text="Integration Bee",
            bbox=[100, 400, 800, 520],
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (10.0, 28.0, 45.0, 38.0, "Integration"),
                (47.0, 28.0, 60.0, 38.0, "Bee"),
                (10.0, 70.0, 45.0, 80.0, "Integration"),
                (47.0, 70.0, 60.0, 80.0, "Bee"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertLess(planned.visible_fragments[0].bbox[1], 400)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)

    def test_shifted_box_repair_rejects_ambiguous_repeated_short_fragment(self) -> None:
        original_bbox = [100.0, 400.0, 800.0, 520.0]
        planned = PageElement(
            role=ElementRole.H2,
            visible_fragments=[
                TextFragment(text="Integration Bee", bbox=list(original_bbox))
            ],
            visible_text="Integration Bee",
            accessible_text="Integration Bee",
            bbox=list(original_bbox),
        )
        chunks = _page_anchor_chunks([planned])[0]

        _align_element_fragments(
            planned,
            chunks,
            [
                (10.0, 28.0, 45.0, 38.0, "Integration"),
                (47.0, 28.0, 60.0, 38.0, "Bee"),
                (10.0, 54.0, 45.0, 64.0, "Integration"),
                (47.0, 54.0, 60.0, 64.0, "Bee"),
            ],
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertEqual(planned.visible_fragments[0].bbox, original_bbox)

    def test_shifted_box_repair_selects_local_repeated_long_fragment(self) -> None:
        planned = PageElement(
            role=ElementRole.P,
            visible_fragments=[
                TextFragment(
                    text="Presented by: Konstantina Christodoulopoulou",
                    bbox=[100, 700, 950, 800],
                )
            ],
            visible_text="Presented by: Konstantina Christodoulopoulou",
            accessible_text="Presented by: Konstantina Christodoulopoulou",
            bbox=[100, 700, 950, 800],
        )
        chunks = _page_anchor_chunks([planned])[0]
        phrase = ["Presented", "by:", "Konstantina", "Christodoulopoulou"]
        words = []
        for top in (5.0, 44.0):
            for index, word in enumerate(phrase):
                words.append(
                    (10.0 + index * 20, top, 28.0 + index * 20, top + 8.0, word)
                )

        placements = _align_element_fragments(
            planned,
            chunks,
            words,
            width=100,
            height=100,
            geometry_source="native",
        )

        self.assertGreater(planned.visible_fragments[0].bbox[1], 400)
        self.assertEqual(planned.visible_fragments[0].alignment_coverage, 1)
        self.assertTrue(
            all(
                placement.bbox[1] >= 40
                for items in placements.values()
                for placement in items
            )
        )

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
            pdf2_draft = Path(temp) / "draft-v2.pdf"
            with pikepdf.Pdf.open(draft) as pdf:
                pdf.save(pdf2_draft, force_version="2.0")
            draft = pdf2_draft
            self.assertTrue(draft.read_bytes().startswith(b"%PDF-2.0"))

            plan = DocumentPlan(
                source_file="source.pdf",
                source_sha256="a" * 64,
                title="Test",
                pages=[],
            )
            source_metadata = SourceMetadata(
                author="Ada Lovelace",
                subject="Analytical Engine",
                keywords="mathematics; history",
                xmp_authors=["Ada Lovelace"],
                description="A history of the Analytical Engine.",
                xmp_keywords="mathematics; history",
                creation_date="D:20000102030405",
                xmp_creation_date="2000-01-02T03:04:05",
                encoding_software=["TeX", "Ghostscript 5.50"],
            )
            add_pdfua_declaration(
                draft,
                candidate,
                plan,
                datetime(2026, 7, 19, 12, 30, tzinfo=timezone.utc),
                source_metadata,
            )
            self.assertTrue(candidate.read_bytes().startswith(b"%PDF-1.7"))

            with pikepdf.Pdf.open(draft) as pdf:
                with pdf.open_metadata() as metadata:
                    self.assertNotIn("pdfuaid:part", metadata)
            with pikepdf.Pdf.open(candidate) as pdf:
                self.assertNotIn(pikepdf.Name.Creator, pdf.docinfo)
                self.assertEqual(str(pdf.docinfo.Producer), REMEDIATION_PRODUCER)
                self.assertEqual(str(pdf.docinfo.Remediation), REMEDIATION_SUMMARY)
                self.assertEqual(
                    str(pdf.docinfo[pikepdf.Name("/Original encoding software")]),
                    "TeX; Ghostscript 5.50",
                )
                self.assertEqual(str(pdf.docinfo.Author), "Ada Lovelace")
                self.assertEqual(str(pdf.docinfo.Subject), "Analytical Engine")
                self.assertEqual(str(pdf.docinfo.Keywords), "mathematics; history")
                self.assertEqual(str(pdf.docinfo.CreationDate), "D:20000102030405")
                with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
                    self.assertEqual(metadata["pdfuaid:part"], "1")
                    self.assertEqual(metadata["llmpr:tool"], REMEDIATION_TOOL)
                    self.assertEqual(metadata["llmpr:version"], "0.5.0")
                    self.assertEqual(
                        metadata["llmpr:remediationDate"], "2026-07-19T12:30:00Z"
                    )
                    self.assertEqual(metadata["llmpr:schemaVersion"], "7")
                    self.assertEqual(metadata["llmpr:sourceSha256"], "a" * 64)
                    self.assertIn("llmpr:canonicalPlanSha256", metadata)
                    self.assertEqual(metadata["llmpr:remediation"], REMEDIATION_SUMMARY)
                    self.assertEqual(
                        metadata["llmpr:originalEncodingSoftware"],
                        "TeX; Ghostscript 5.50",
                    )
                    self.assertNotIn("xmp:CreatorTool", metadata)
                    self.assertEqual(metadata["dc:creator"], ["Ada Lovelace"])
                    self.assertEqual(
                        metadata["dc:description"],
                        "A history of the Analytical Engine.",
                    )
                    self.assertEqual(metadata["pdf:Keywords"], "mathematics; history")
                    self.assertEqual(metadata["xmp:CreateDate"], "2000-01-02T03:04:05")
            report = validate_output(
                draft,
                candidate,
                plan,
                source_metadata=source_metadata,
            )
            self.assertTrue(report["remediation_metadata_valid"])

    def test_absent_xmp_description_remains_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            draft = Path(temp) / "draft.pdf"
            candidate = Path(temp) / "candidate.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(draft)
            document.close()
            plan = DocumentPlan(
                source_file="source.pdf",
                source_sha256="a" * 64,
                title="Test",
                pages=[],
            )
            source_metadata = SourceMetadata()
            add_pdfua_declaration(
                draft,
                candidate,
                plan,
                source_metadata=source_metadata,
            )
            report = validate_output(
                draft,
                candidate,
                plan,
                source_metadata=source_metadata,
            )
            self.assertEqual(report["remediation_metadata"]["description"], "")
            self.assertTrue(report["remediation_metadata_valid"])


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

            self.assertEqual(plan.schema_version, 7)
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

            self.assertEqual(plan.schema_version, 7)
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


class RecentTitleTests(unittest.TestCase):
    def test_local_history_preserves_all_titles_and_loads_recent_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".recent-titles.json"

            self.assertEqual(load_recent_titles(path), [])
            self.assertTrue(path.exists())
            remember_title(path, "First Document", limit=2)
            remember_title(path, "Second Document", limit=2)
            remember_title(path, "First Document", limit=2)
            remember_title(path, "Third Document", limit=2)

            self.assertEqual(
                load_recent_titles(path, limit=2),
                ["First Document", "Third Document"],
            )
            self.assertEqual(
                load_recent_titles(path),
                ["Second Document", "First Document", "Third Document"],
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["titles"],
                ["Second Document", "First Document", "Third Document"],
            )

    def test_concurrent_history_updates_do_not_lose_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".recent-titles.json"
            titles = [f"Concurrent Document {index}" for index in range(24)]

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda title: remember_title(path, title), titles))

            self.assertCountEqual(load_recent_titles(path), titles)


class DeterministicRefinementTests(unittest.TestCase):
    def test_missing_blank_table_cells_are_completed_idempotently(self) -> None:
        def table_cell(
            role: ElementRole,
            text: str,
            row: int,
            column: int,
            scope: TableHeaderScope | None = None,
        ) -> PageElement:
            return PageElement(
                role=role,
                visible_text=text,
                accessible_text=text,
                table_id="exam-grid",
                table_row=row,
                table_column=column,
                header_scope=scope,
                bbox=[100 + column * 200, 100 + row * 100, 300 + column * 200, 200 + row * 100],
                review_status=ReviewStatus.MODEL_REVIEWED,
            )

        page = PagePlan(
            page_number=1,
            elements=[
                table_cell(ElementRole.TH, "January", 0, 1, TableHeaderScope.COLUMN),
                table_cell(ElementRole.TH, "May", 0, 2, TableHeaderScope.COLUMN),
                table_cell(ElementRole.TH, "Attempt", 1, 0, TableHeaderScope.ROW),
                table_cell(ElementRole.TD, "", 1, 1),
                table_cell(ElementRole.TD, "", 1, 2),
            ],
            review_status=ReviewStatus.MODEL_REVIEWED,
        )

        _complete_rectangular_table_grids(page)
        first_result = page.model_dump(mode="json")
        _complete_rectangular_table_grids(page)

        self.assertEqual(page.model_dump(mode="json"), first_result)
        self.assertEqual(
            [(element.table_row, element.table_column) for element in page.elements],
            [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)],
        )
        self.assertEqual(page.elements[0].role, ElementRole.TD)
        self.assertEqual(page.elements[0].visible_text, "")
        self.assertEqual(len(page.findings), 1)

    def test_reviewed_fixed_form_markers_are_omitted_from_spoken_text(self) -> None:
        visible = "(Co-Chair? *__Yes__ No)"
        accessible = "(Co-Chair? Yes No)"
        row = PageElement(
            role=ElementRole.P,
            visible_text=visible,
            accessible_text=accessible,
            transformations=[
                TextTransformation(
                    kind=TransformationKind.DECORATIVE_MARKER_OMISSION,
                    source_text=visible,
                    replacement_text=accessible,
                    rationale="Reviewed fixed-form blank markers.",
                )
            ],
            bbox=[100, 100, 500, 150],
        )

        canonicalize_transformations(row)

        self.assertEqual(row.accessible_text, accessible)
        self.assertTrue(row.transformations)
        self.assertFalse(
            any(
                item.kind == TransformationKind.UNVERIFIED
                for item in row.transformations
            )
        )

    def test_static_form_rules_and_empty_boxes_are_silenced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            visible = (
                "Student Name: __________ UFID______-______ "
                "Location: ☐ In-person ☐ Via Zoom ☐ Hybrid"
            )
            row = PageElement(
                role=ElementRole.P,
                visible_text=visible,
                accessible_text=visible,
                bbox=[100, 100, 900, 180],
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Static form",
                pages=[PagePlan(page_number=1, elements=[row])],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                row.accessible_text,
                "Student Name: UFID Location: In-person Via Zoom Hybrid",
            )
            self.assertFalse(
                any(
                    item.kind == TransformationKind.UNVERIFIED
                    for item in row.transformations
                )
            )

    def test_static_rules_do_not_discard_reviewed_fraction_speech(self) -> None:
        visible = "1. 0 to 1/3 full _____ 1/3 to 2/3 full _____"
        accessible = "1. 0 to one third full; one third to two thirds full"
        row = PageElement(
            role=ElementRole.LI,
            visible_text=visible,
            accessible_text=accessible,
            transformations=[
                TextTransformation(
                    kind=TransformationKind.FORMULA_SPOKEN_EQUIVALENT,
                    source_text="0 to 1/3 full",
                    replacement_text="0 to one third full",
                    rationale="Reviewed fraction speech.",
                ),
                TextTransformation(
                    kind=TransformationKind.FORMULA_SPOKEN_EQUIVALENT,
                    source_text="1/3 to 2/3 full",
                    replacement_text="one third to two thirds full",
                    rationale="Reviewed fraction speech.",
                ),
            ],
            bbox=[100, 100, 900, 180],
        )

        _normalize_static_form_markers(row)
        canonicalize_transformations(row)

        self.assertEqual(row.accessible_text, accessible)
        self.assertEqual(len(row.formula_spans), 2)
        self.assertFalse(
            any(finding.severity == ReviewSeverity.CRITICAL for finding in row.findings)
        )
        self.assertFalse(
            any(
                item.kind == TransformationKind.UNVERIFIED
                for item in row.transformations
            )
        )

    def test_formula_mapping_tolerates_layout_line_breaks(self) -> None:
        visible = "The estimate assumes 1 << M\n<< N."
        spoken = "one is much less than M, which is much less than N"
        paragraph = PageElement(
            role=ElementRole.P,
            visible_text=visible,
            accessible_text=f"The estimate assumes {spoken}.",
            transformations=[
                TextTransformation(
                    kind=TransformationKind.FORMULA_SPOKEN_EQUIVALENT,
                    source_text="1 << M << N",
                    replacement_text=spoken,
                    rationale="Reviewed spoken inequality.",
                )
            ],
            bbox=[100, 100, 900, 200],
        )

        canonicalize_transformations(paragraph)

        formulae = paragraph.formula_spans
        self.assertEqual(len(formulae), 1)
        self.assertEqual(formulae[0].text, "1 << M\n<< N")
        self.assertEqual(formulae[0].alt_text, spoken)
        self.assertEqual(paragraph.extraction_text, visible)
        self.assertFalse(
            any(
                finding.severity == ReviewSeverity.CRITICAL
                for finding in paragraph.findings
            )
        )

    def test_numeric_hyphen_to_spoken_range_is_approved(self) -> None:
        heading = element(
            ElementRole.H3,
            "Honors Teacher of the Year 2011-2012",
        )
        heading.accessible_text = "Honors Teacher of the Year 2011 to 2012"

        canonicalize_transformations(heading)

        self.assertEqual(
            heading.transformations[0].kind,
            TransformationKind.DATE_RANGE_EXPANSION,
        )
        self.assertFalse(
            any(
                finding.severity == ReviewSeverity.CRITICAL
                for finding in heading.findings
            )
        )

    def test_compact_roster_separators_are_declared_omissions(self) -> None:
        item = element(ElementRole.LI, "and Brianna Henry")
        item.accessible_text = "Brianna Henry"

        canonicalize_transformations(item)

        self.assertEqual(
            item.transformations[0].kind,
            TransformationKind.INLINE_LIST_SEPARATOR_OMISSION,
        )
        self.assertFalse(
            any(
                finding.severity == ReviewSeverity.CRITICAL
                for finding in item.findings
            )
        )

        trailing = element(ElementRole.LI, "Robert Monahan and")
        trailing.accessible_text = "Robert Monahan"
        canonicalize_transformations(trailing)
        self.assertEqual(
            trailing.transformations[0].kind,
            TransformationKind.INLINE_LIST_SEPARATOR_OMISSION,
        )

        pipe = element(ElementRole.LI, "Alexa Panos |")
        pipe.accessible_text = "Alexa Panos"
        canonicalize_transformations(pipe)
        self.assertEqual(
            pipe.transformations[0].kind,
            TransformationKind.INLINE_LIST_SEPARATOR_OMISSION,
        )

    def test_visual_pipe_can_be_normalized_to_spoken_punctuation(self) -> None:
        caption = element(ElementRole.P, "Node Chair | Steelcase")
        caption.accessible_text = "Node Chair, Steelcase"

        canonicalize_transformations(caption)

        self.assertEqual(
            caption.transformations[0].kind,
            TransformationKind.STRUCTURAL_SEPARATOR_NORMALIZATION,
        )

    def test_undeclared_math_rewrite_is_reverted_and_blocks_release(self) -> None:
        paragraph = PageElement(
            role=ElementRole.P,
            visible_text="Let p = p₁p₂...pₙ.",
            accessible_text="Let p equals p subscript 1 pₙ.",
            bbox=[100, 100, 900, 200],
        )

        canonicalize_transformations(paragraph)

        self.assertEqual(paragraph.accessible_text, paragraph.visible_text)
        self.assertEqual(paragraph.transformations, [])
        self.assertTrue(
            any(
                finding.severity == ReviewSeverity.CRITICAL
                and finding.category == FindingCategory.FORMULA
                for finding in paragraph.findings
            )
        )

    def test_reviewer_confirmed_ornament_is_a_declared_omission(self) -> None:
        quotation = element(ElementRole.P, "Quotation ##")
        quotation.accessible_text = "Quotation"
        quotation.findings.append(
            ReviewFinding(
                severity=ReviewSeverity.WARNING,
                category=FindingCategory.DECORATION,
                message="The ornamental terminal marks are omitted from accessible text.",
                chosen="Omit using a decorative-marker transformation",
            )
        )

        canonicalize_transformations(quotation)

        self.assertEqual(
            quotation.transformations[0].kind,
            TransformationKind.DECORATIVE_MARKER_OMISSION,
        )
        self.assertFalse(
            any(
                finding.severity == ReviewSeverity.CRITICAL
                for finding in quotation.findings
            )
        )

    def test_resolved_proposal_role_disagreement_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            finding = ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.SEMANTIC_ROLE,
                message=(
                    "The first-model proposal grouped roster entries incorrectly; "
                    "the canonical plan supplies one LI per item."
                ),
                chosen="One LI per roster item",
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Roster",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[element(ElementRole.P, "Roster")],
                        findings=[finding],
                    )
                ],
            )

            refine_document_plan(source, plan)

            resolved = plan.pages[0].findings[0]
            self.assertEqual(resolved.severity, ReviewSeverity.INFO)
            self.assertEqual(resolved.category, FindingCategory.MODEL_DISAGREEMENT)
            self.assertTrue(resolved.chosen.startswith("Resolved in canonical plan:"))

    def test_resolved_first_model_geometry_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            finding = ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.GEOMETRY,
                message=(
                    "The first model placed table-cell fragments substantially to the "
                    "right of their printed columns. Canonical geometry now follows "
                    "the visible columns."
                ),
                chosen="Use image-derived table columns with native row evidence.",
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Program",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[element(ElementRole.P, "Program")],
                        findings=[finding],
                    )
                ],
            )

            refine_document_plan(source, plan)

            resolved = plan.pages[0].findings[0]
            self.assertEqual(resolved.severity, ReviewSeverity.INFO)
            self.assertEqual(
                resolved.chosen,
                "Resolved: canonical geometry is normalized and evidence-derived.",
            )

    def test_resolved_image_alignment_disagreement_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            finding = ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.SEMANTIC_ROLE,
                message=(
                    "The first proposal attached the designation to the wrong name. "
                    "Image row alignment places it beside the second name."
                ),
                chosen="Second name with designation",
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Roster",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[element(ElementRole.P, "Roster")],
                        findings=[finding],
                    )
                ],
            )

            refine_document_plan(source, plan)

            self.assertEqual(plan.pages[0].findings[0].severity, ReviewSeverity.INFO)

    def test_resolved_first_model_name_error_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            finding = ReviewFinding(
                severity=ReviewSeverity.CRITICAL,
                category=FindingCategory.NAME,
                message=(
                    "The image and both evidence streams read William; "
                    "the first-model proposal omitted a letter."
                ),
                chosen="William",
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Roster",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[element(ElementRole.P, "William")],
                        findings=[finding],
                    )
                ],
            )

            refine_document_plan(source, plan)

            resolved = plan.pages[0].findings[0]
            self.assertEqual(resolved.severity, ReviewSeverity.INFO)
            self.assertEqual(resolved.category, FindingCategory.MODEL_DISAGREEMENT)


    def test_isolated_unmarked_list_item_becomes_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()
            plan = DocumentPlan(
                source_file=source.name,
                title="Awards",
                pages=[
                    PagePlan(
                        page_number=1,
                        elements=[
                            element(ElementRole.H2, "Award"),
                            element(ElementRole.LI, "Ada Lovelace", top=200),
                            element(ElementRole.H2, "Participants", top=300),
                            element(ElementRole.LI, "Emmy Noether", top=400),
                            element(ElementRole.LI, "Sofia Kovalevskaya", top=500),
                        ],
                    )
                ],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                [item.role for item in plan.pages[0].elements],
                [
                    ElementRole.H2,
                    ElementRole.P,
                    ElementRole.H2,
                    ElementRole.LI,
                    ElementRole.LI,
                ],
            )
            self.assertTrue(
                any(
                    "one-item list" in finding.message
                    for finding in plan.pages[0].elements[1].findings
                )
            )

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
                            element(ElementRole.P, "A DEPARTMENTAL PUBLICATION", top=80),
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
                    "A DEPARTMENTAL PUBLICATION",
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

    def test_moves_reviewed_empty_alt_decoration_to_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            document = pymupdf.open()
            document.new_page(width=612, height=792)
            document.save(source)
            document.close()
            flourish = PageElement(
                role=ElementRole.FIGURE,
                alt_text=None,
                bbox=[0, 0, 1000, 100],
                findings=[
                    ReviewFinding(
                        severity=ReviewSeverity.INFO,
                        category=FindingCategory.DECORATION,
                        message="The page-wide banner is decorative.",
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
            marked_heading = PageElement(
                role=ElementRole.H2,
                visible_text="* ATRIUM ROOM",
                accessible_text="* ATRIUM ROOM",
                bbox=[100, 650, 900, 700],
                review_status=ReviewStatus.MODEL_REVIEWED,
                findings=[
                    ReviewFinding(
                        severity=ReviewSeverity.WARNING,
                        category=FindingCategory.TRANSCRIPTION,
                        message="The reviewed heading omits its visual marker.",
                        chosen="ATRIUM ROOM",
                    )
                ],
            )
            marked_list_item = PageElement(
                role=ElementRole.LI,
                visible_text="- seating group 1",
                accessible_text="seating group 1",
                bbox=[100, 750, 900, 800],
                review_status=ReviewStatus.MODEL_REVIEWED,
            )
            plan = DocumentPlan(
                source_file=source.name,
                title="Sample",
                pages=[
                    PagePlan(
                        page_number=1,
                        coordinate_space=CoordinateSpace.NORMALIZED,
                        elements=[
                            dehyphenated,
                            unverified,
                            date_range,
                            marked_heading,
                            marked_list_item,
                        ],
                    )
                ],
            )

            refine_document_plan(source, plan)

            self.assertEqual(
                dehyphenated.transformations[0].kind,
                TransformationKind.LINE_BREAK_DEHYPHENATION,
            )
            self.assertEqual(unverified.accessible_text, "sucess")
            self.assertEqual(unverified.transformations, [])
            self.assertTrue(
                any(
                    finding.category == FindingCategory.TRANSFORMATION
                    and finding.severity == ReviewSeverity.INFO
                    for finding in unverified.findings
                )
            )
            self.assertEqual(date_range.visible_text, "September 15–17, 2006.")
            self.assertEqual(date_range.accessible_text, "September 15 to 17, 2006.")
            self.assertEqual(
                date_range.transformations[0].kind,
                TransformationKind.DATE_RANGE_EXPANSION,
            )
            self.assertEqual(marked_heading.accessible_text, "ATRIUM ROOM")
            self.assertEqual(
                marked_heading.transformations[0].kind,
                TransformationKind.DECORATIVE_MARKER_OMISSION,
            )
            self.assertEqual(marked_list_item.accessible_text, "seating group 1")
            self.assertEqual(
                marked_list_item.transformations[0].kind,
                TransformationKind.DECORATIVE_MARKER_OMISSION,
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
