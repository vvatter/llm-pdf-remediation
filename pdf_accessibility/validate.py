from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path
import re
import shutil
import subprocess

import pikepdf
import pymupdf
from pikepdf import Name

from .models import DocumentPlan, ElementRole, ReviewStatus
from .preflight import run_verapdf
from .refine import transformation_errors


EXPECTED_PDF_ROLES = {
    ElementRole.DOCUMENT_TITLE: "/H1",
    ElementRole.H1: "/H1",
    ElementRole.H2: "/H2",
    ElementRole.H3: "/H3",
    ElementRole.P: "/P",
    ElementRole.FIGURE: "/Figure",
}


def plan_is_approved(plan: DocumentPlan) -> bool:
    approved = {ReviewStatus.MODEL_REVIEWED, ReviewStatus.MANUAL_MODIFIED}
    return plan.review_status in approved and all(
        page.review_status in approved
        and all(element.review_status in approved for element in page.elements)
        for page in plan.pages
    )


def _render_hashes(path: Path, dpi: int = 120) -> list[str]:
    hashes: list[str] = []
    with pymupdf.open(path) as document:
        matrix = pymupdf.Matrix(dpi / 72, dpi / 72)
        for page in document:
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            hashes.append(hashlib.sha256(pixmap.samples).hexdigest())
    return hashes


def _source_visual_fidelity(reference: Path, selected_base: Path) -> dict[str, object]:
    results: dict[str, object] = {}
    dimensions_match = True
    for dpi in (72, 150):
        total_difference = 0
        material_difference = 0
        samples = 0
        page_metrics: list[dict[str, object]] = []
        with pymupdf.open(reference) as reference_document, pymupdf.open(
            selected_base
        ) as base_document:
            dimensions_match = dimensions_match and (
                reference_document.page_count == base_document.page_count
            )
            matrix = pymupdf.Matrix(dpi / 72, dpi / 72)
            for page_number, (reference_page, base_page) in enumerate(
                zip(reference_document, base_document), start=1
            ):
                reference_pixmap = reference_page.get_pixmap(
                    matrix=matrix, colorspace=pymupdf.csRGB, alpha=False
                )
                base_pixmap = base_page.get_pixmap(
                    matrix=matrix, colorspace=pymupdf.csRGB, alpha=False
                )
                if (
                    reference_pixmap.width != base_pixmap.width
                    or reference_pixmap.height != base_pixmap.height
                ):
                    dimensions_match = False
                    continue
                # Sample every eighth RGB pixel. This is deterministic and keeps the
                # two-DPI archival comparison inexpensive on long documents.
                reference_samples = reference_pixmap.samples
                base_samples = base_pixmap.samples
                page_difference = 0
                page_material_difference = 0
                page_samples = 0
                for index in range(0, min(len(reference_samples), len(base_samples)), 24):
                    for channel in range(3):
                        difference = abs(
                            reference_samples[index + channel]
                            - base_samples[index + channel]
                        )
                        total_difference += difference
                        material_difference += difference > 16
                        samples += 1
                        page_difference += difference
                        page_material_difference += difference > 16
                        page_samples += 1
                page_metrics.append(
                    {
                        "page": page_number,
                        "mean_absolute_channel_difference": round(
                            page_difference / page_samples / 255, 6
                        ),
                        "sample_fraction_over_16": round(
                            page_material_difference / page_samples, 6
                        ),
                    }
                )
        results[str(dpi)] = {
            "mean_absolute_channel_difference": round(
                total_difference / samples / 255, 6
            )
            if samples
            else None,
            "sample_fraction_over_16": round(material_difference / samples, 6)
            if samples
            else None,
            "maximum_page_mean_absolute_channel_difference": max(
                (
                    item["mean_absolute_channel_difference"]
                    for item in page_metrics
                ),
                default=None,
            ),
            "maximum_page_sample_fraction_over_16": max(
                (item["sample_fraction_over_16"] for item in page_metrics),
                default=None,
            ),
            "pages": page_metrics,
        }
    means = [
        item["maximum_page_mean_absolute_channel_difference"]
        for item in results.values()
        if item["maximum_page_mean_absolute_channel_difference"] is not None
    ]
    fractions = [
        item["maximum_page_sample_fraction_over_16"]
        for item in results.values()
        if item["maximum_page_sample_fraction_over_16"] is not None
    ]
    within_tolerance = bool(
        dimensions_match
        and means
        and max(means) <= 0.05
        and max(fractions) <= 0.25
    )
    return {
        "dimensions_match": dimensions_match,
        "sampled_dpi": results,
        "thresholds": {
            "maximum_mean_absolute_channel_difference": 0.05,
            "maximum_sample_fraction_over_16": 0.25,
        },
        "within_tolerance": within_tolerance,
    }


def _actual_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _page_mcids(pdf: pikepdf.Pdf) -> tuple[dict[int, dict[int, str]], list[str]]:
    pages: dict[int, dict[int, str]] = {}
    errors: list[str] = []
    for page_number, page in enumerate(pdf.pages, start=1):
        mcids: dict[int, str] = {}
        stack: list[tuple[bool, int | None]] = []
        try:
            instructions = pikepdf.parse_content_stream(page)
        except (pikepdf.PdfError, ValueError) as error:
            errors.append(f"page {page_number}: cannot parse content stream: {error}")
            pages[page_number] = mcids
            continue
        for instruction in instructions:
            operator = str(instruction.operator)
            operands = instruction.operands
            if operator == "BMC":
                stack.append((str(operands[0]) == "/Artifact", None))
            elif operator == "BDC":
                properties = operands[1] if len(operands) > 1 else None
                is_artifact = str(operands[0]) == "/Artifact"
                mcid = None
                if isinstance(properties, pikepdf.Dictionary) and "/MCID" in properties:
                    mcid = int(properties.MCID)
                    if any(item[0] for item in stack) or is_artifact:
                        errors.append(f"page {page_number} MCID {mcid} is nested in an artifact")
                    if mcid in mcids:
                        errors.append(f"page {page_number} has duplicate MCID {mcid}")
                    mcids[mcid] = _actual_text(properties.get("/ActualText"))
                stack.append((is_artifact, mcid))
            elif operator == "EMC":
                if stack:
                    stack.pop()
                else:
                    errors.append(f"page {page_number} has unmatched EMC")
        if stack:
            errors.append(f"page {page_number} has unclosed marked-content sequences")
        pages[page_number] = mcids
    return pages, errors


def serialize_structure_tree(pdf_path: Path) -> dict[str, object]:
    records: list[dict[str, object]] = []
    errors: list[str] = []
    with pikepdf.Pdf.open(pdf_path) as pdf:
        if "/StructTreeRoot" not in pdf.Root:
            return {"elements": [], "errors": ["missing StructTreeRoot"]}
        page_mcids, content_errors = _page_mcids(pdf)
        errors.extend(content_errors)
        page_numbers = {page.obj.objgen: index for index, page in enumerate(pdf.pages, start=1)}
        root = pdf.Root.StructTreeRoot
        parent_tree_by_key: dict[int, object] = {}
        nums = list(root.get("/ParentTree", {}).get("/Nums", []))
        for index in range(0, len(nums), 2):
            parent_tree_by_key[int(nums[index])] = nums[index + 1]

        document = root.get("/K")
        children = document.get("/K", []) if isinstance(document, pikepdf.Dictionary) else []
        if isinstance(children, pikepdf.Dictionary):
            children = [children]
        referenced: set[tuple[int, int]] = set()
        for element_index, element in enumerate(children):
            role = str(element.get("/S", ""))
            content_items = element.get("/K", [])
            if isinstance(content_items, pikepdf.Dictionary):
                content_items = [content_items]
            chunks: list[str] = []
            mcrs: list[dict[str, int]] = []
            for content_item in content_items:
                if not isinstance(content_item, pikepdf.Dictionary) or "/MCID" not in content_item:
                    errors.append(f"structure element {element_index} has a non-MCR child")
                    continue
                page_obj = content_item.get("/Pg", element.get("/Pg"))
                page_number = page_numbers.get(page_obj.objgen) if page_obj is not None else None
                mcid = int(content_item.MCID)
                if page_number is None:
                    errors.append(f"structure element {element_index} MCR has no resolvable page")
                    continue
                key = (page_number, mcid)
                if key in referenced:
                    errors.append(f"page {page_number} MCID {mcid} is referenced more than once")
                referenced.add(key)
                text = page_mcids.get(page_number, {}).get(mcid)
                if text is None:
                    errors.append(f"structure element {element_index} references missing page {page_number} MCID {mcid}")
                    text = ""
                chunks.append(text)
                mcrs.append({"page": page_number, "mcid": mcid})

                struct_parent = int(pdf.pages[page_number - 1].obj.get("/StructParents", -1))
                parent_array = parent_tree_by_key.get(struct_parent)
                if not isinstance(parent_array, pikepdf.Array) or mcid >= len(parent_array):
                    errors.append(f"page {page_number} MCID {mcid} is absent from ParentTree")
                elif parent_array[mcid].objgen != element.objgen:
                    errors.append(f"page {page_number} MCID {mcid} ParentTree points elsewhere")

            alt = str(element.get("/Alt", ""))
            text = "".join(chunks)
            if role == "/Figure" and not alt.strip():
                errors.append(f"figure structure element {element_index} has no alternate text")
            if not text and not (role == "/Figure" and alt):
                errors.append(f"structure element {element_index} is empty")
            records.append({"role": role, "text": text, "alt_text": alt, "mcrs": mcrs})

        for page_number, mcids in page_mcids.items():
            for mcid in mcids:
                if (page_number, mcid) not in referenced:
                    errors.append(f"page {page_number} MCID {mcid} is not referenced by the structure tree")
    return {"elements": records, "errors": errors}


def compare_structure_to_plan(serialized: dict[str, object], plan: DocumentPlan) -> list[str]:
    errors: list[str] = list(serialized.get("errors", []))
    actual = list(serialized.get("elements", []))
    expected = [element for page in plan.pages for element in page.elements]
    if len(actual) != len(expected):
        errors.append(f"structure has {len(actual)} elements; plan has {len(expected)}")
    for index, (record, element) in enumerate(zip(actual, expected, strict=False)):
        expected_role = EXPECTED_PDF_ROLES[element.role]
        if record["role"] != expected_role:
            errors.append(f"element {index} role {record['role']} != {expected_role}")
        if record["text"] != element.semantic_text:
            errors.append(f"element {index} exact text does not match canonical plan")
        if element.role == ElementRole.FIGURE and record["alt_text"] != (element.alt_text or ""):
            errors.append(f"figure {index} alternate text does not match canonical plan")
    return errors


def _extraction_compatibility(
    output: Path, plan: DocumentPlan | None
) -> dict[str, object]:
    executable = shutil.which("pdftotext")
    if not executable or plan is None:
        return {
            "pdftotext_available": bool(executable),
            "extraction_token_agreement": None,
            "extraction_token_count_ratio": None,
            "extraction_compatible": plan is None,
        }
    completed = subprocess.run(
        [executable, "-raw", "-enc", "UTF-8", str(output), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    expected_text = "\n".join(
        element.semantic_text for page in plan.pages for element in page.elements
    )
    expected_tokens = re.findall(r"\w+|[^\w\s]", expected_text.lower(), re.UNICODE)
    actual_tokens = re.findall(r"\w+|[^\w\s]", completed.stdout.lower(), re.UNICODE)
    if expected_tokens:
        agreement = SequenceMatcher(
            None, expected_tokens, actual_tokens, autojunk=False
        ).ratio()
        count_ratio = len(actual_tokens) / len(expected_tokens)
    else:
        agreement = 1.0 if not actual_tokens else 0.0
        count_ratio = 1.0 if not actual_tokens else float("inf")
    compatible = (
        completed.returncode == 0
        and agreement >= 0.99
        and 0.98 <= count_ratio <= 1.02
    )
    return {
        "pdftotext_available": True,
        "pdftotext_returncode": completed.returncode,
        "extraction_token_agreement": round(agreement, 6),
        "extraction_token_count_ratio": round(count_ratio, 6),
        "extraction_compatible": compatible,
    }


def validate_output(
    source: Path,
    output: Path,
    plan: DocumentPlan | None = None,
    reference_source: Path | None = None,
) -> dict[str, object]:
    before = _render_hashes(source)
    after = _render_hashes(output)
    qpdf = subprocess.run(
        ["qpdf", "--check", str(output)], capture_output=True, text=True, check=False
    )
    serialized = serialize_structure_tree(output)
    structure_errors = compare_structure_to_plan(serialized, plan) if plan else serialized["errors"]
    extraction = _extraction_compatibility(output, plan)
    plan_transformation_errors = transformation_errors(plan) if plan else []
    with pikepdf.Pdf.open(output) as pdf:
        has_structure_tree = "/StructTreeRoot" in pdf.Root
        marked = bool(pdf.Root.get("/MarkInfo", {}).get("/Marked", False))
        tagged_pages = sum("/StructParents" in page.obj for page in pdf.pages)
        language = str(pdf.Root.get("/Lang", ""))
        title = str(pdf.docinfo.get("/Title", ""))
        with pdf.open_outline() as outline:
            bookmark_count = len(outline.root)
        with pdf.open_metadata() as metadata:
            declares_pdfua = metadata.get("pdfuaid:part") == "1"
        page_count = len(pdf.pages)
        page_labels_present = "/PageLabels" in pdf.Root
    source_fidelity = (
        _source_visual_fidelity(reference_source, source)
        if reference_source
        else None
    )
    return {
        "source": str(source),
        "output": str(output),
        "visual_match": before == after,
        "page_count_match": len(before) == len(after),
        "qpdf_ok": qpdf.returncode in {0, 3},
        "qpdf_output": (qpdf.stdout + qpdf.stderr).strip(),
        "has_structure_tree": has_structure_tree,
        "marked": marked,
        "tagged_pages": tagged_pages,
        "fully_tagged": tagged_pages == page_count,
        "language": language,
        "title": title,
        "structure_elements": len(serialized["elements"]),
        "structure_errors": structure_errors,
        "structure_matches_plan": not structure_errors,
        "all_elements_have_accessible_text": not any("empty" in error for error in structure_errors),
        "bookmark_count": bookmark_count,
        "declares_pdfua": declares_pdfua,
        "page_labels_present": page_labels_present,
        "transformation_errors": plan_transformation_errors,
        "transformations_valid": not plan_transformation_errors,
        "source_visual_fidelity": source_fidelity,
        "source_visual_fidelity_ok": (
            source_fidelity["within_tolerance"] if source_fidelity else True
        ),
        **extraction,
    }


def add_pdfua_declaration(draft: Path, candidate: Path) -> None:
    if candidate.exists():
        candidate.unlink()
    with pikepdf.Pdf.open(draft) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
            metadata["pdfuaid:part"] = "1"
        pdf.save(candidate, min_version="1.4", linearize=True)


def release_pdfua(
    source: Path,
    draft: Path,
    output: Path,
    plan: DocumentPlan,
    reference_source: Path | None = None,
) -> dict[str, object]:
    candidate = output.with_suffix(".candidate.pdf")
    add_pdfua_declaration(draft, candidate)
    report = validate_output(
        source, candidate, plan, reference_source=reference_source
    )
    vera_ok, vera_report = run_verapdf(candidate)
    report["verapdf_pdfua_ok"] = vera_ok
    report["verapdf_report"] = vera_report
    plan_approved = plan_is_approved(plan)
    report["plan_approved"] = plan_approved
    machine_ok = all(
        [
            report["visual_match"],
            report["source_visual_fidelity_ok"],
            report["page_count_match"],
            report["qpdf_ok"],
            report["structure_matches_plan"],
            report["fully_tagged"],
            report["page_labels_present"],
            report["transformations_valid"],
            report["extraction_compatible"],
            report["declares_pdfua"],
            vera_ok is True,
            plan_approved,
        ]
    )
    report["released"] = machine_ok
    if machine_ok:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(candidate, output)
    elif candidate.exists():
        candidate.unlink()
    return report


def write_validation_report(
    source: Path,
    output: Path,
    report_path: Path,
    plan: DocumentPlan | None = None,
) -> dict[str, object]:
    report = validate_output(source, output, plan)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
