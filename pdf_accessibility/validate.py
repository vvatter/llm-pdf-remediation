from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import re
import shutil
import subprocess

import pikepdf
import pymupdf
from pikepdf import Name
from pikepdf.models.metadata import PdfMetadata

from . import __version__
from .models import (
    SCHEMA_VERSION,
    DocumentPlan,
    ElementRole,
    ReviewStatus,
    fragment_region_groups,
)
from .plans import plan_sha256
from .preflight import SourceMetadata, read_source_metadata, run_verapdf
from .refine import transformation_errors


REMEDIATION_NAMESPACE = "https://github.com/vvatter/llm-pdf-remediation/ns/1.0/"
REMEDIATION_PREFIX = "llmpr"
REMEDIATION_TOOL = "llm-pdf-remediation"
REMEDIATION_PRODUCER = f"{REMEDIATION_TOOL} {__version__}"
REMEDIATION_INFO_KEY = Name("/Remediation")
ORIGINAL_ENCODING_INFO_KEY = Name("/Original encoding software")
REMEDIATION_SUMMARY = (
    "Remediated with llm-pdf-remediation and ChatGPT 5.6 Sol: added reviewed accessible "
    "text, semantic tags, reading order, bookmarks, and image descriptions while preserving "
    "page appearance."
)
PdfMetadata.register_xml_namespace(REMEDIATION_NAMESPACE, REMEDIATION_PREFIX)


EXPECTED_PDF_ROLES = {
    ElementRole.DOCUMENT_TITLE: "/H1",
    ElementRole.H1: "/H1",
    ElementRole.H2: "/H2",
    ElementRole.H3: "/H3",
    ElementRole.P: "/P",
    ElementRole.LI: "/LI",
    ElementRole.FIGURE: "/Figure",
}


def plan_is_approved(plan: DocumentPlan) -> bool:
    approved = {ReviewStatus.MODEL_REVIEWED, ReviewStatus.MANUAL_MODIFIED}
    return plan.review_status in approved and all(
        page.review_status in approved
        and all(element.review_status in approved for element in page.elements)
        for page in plan.pages
    )


def _apply_remediation_metadata(
    pdf: pikepdf.Pdf,
    plan: DocumentPlan | None,
    remediated_at: datetime | None = None,
    source_metadata: SourceMetadata | None = None,
) -> None:
    source_metadata = source_metadata or SourceMetadata()
    timestamp = (remediated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    iso_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    original_encoding = "; ".join(source_metadata.encoding_software)

    with pdf.open_metadata(
        set_pikepdf_as_editor=False, update_docinfo=False
    ) as metadata:
        metadata["pdfuaid:part"] = "1"
        metadata["pdf:Producer"] = REMEDIATION_PRODUCER
        metadata["xmp:ModifyDate"] = iso_timestamp
        metadata["xmp:MetadataDate"] = iso_timestamp
        metadata[f"{REMEDIATION_PREFIX}:tool"] = REMEDIATION_TOOL
        metadata[f"{REMEDIATION_PREFIX}:version"] = __version__
        metadata[f"{REMEDIATION_PREFIX}:remediationDate"] = iso_timestamp
        metadata[f"{REMEDIATION_PREFIX}:schemaVersion"] = str(SCHEMA_VERSION)
        metadata[f"{REMEDIATION_PREFIX}:remediation"] = REMEDIATION_SUMMARY
        if original_encoding:
            metadata[f"{REMEDIATION_PREFIX}:originalEncodingSoftware"] = original_encoding
        elif f"{REMEDIATION_PREFIX}:originalEncodingSoftware" in metadata:
            del metadata[f"{REMEDIATION_PREFIX}:originalEncodingSoftware"]
        if plan is not None:
            metadata[f"{REMEDIATION_PREFIX}:canonicalPlanSha256"] = plan_sha256(plan)
            if plan.source_sha256:
                metadata[f"{REMEDIATION_PREFIX}:sourceSha256"] = plan.source_sha256
        if "xmp:CreatorTool" in metadata:
            del metadata["xmp:CreatorTool"]
        if "dc:creator" in metadata:
            del metadata["dc:creator"]
        if source_metadata.xmp_authors:
            metadata["dc:creator"] = source_metadata.xmp_authors
        if "dc:description" in metadata:
            del metadata["dc:description"]
        if source_metadata.description:
            metadata["dc:description"] = source_metadata.description
        if "pdf:Keywords" in metadata:
            del metadata["pdf:Keywords"]
        if source_metadata.xmp_keywords:
            metadata["pdf:Keywords"] = source_metadata.xmp_keywords
        if "xmp:CreateDate" in metadata:
            del metadata["xmp:CreateDate"]
        if source_metadata.xmp_creation_date:
            metadata["xmp:CreateDate"] = source_metadata.xmp_creation_date

    pdf.docinfo[Name.Producer] = REMEDIATION_PRODUCER
    pdf.docinfo[REMEDIATION_INFO_KEY] = REMEDIATION_SUMMARY
    if original_encoding:
        pdf.docinfo[ORIGINAL_ENCODING_INFO_KEY] = original_encoding
    elif ORIGINAL_ENCODING_INFO_KEY in pdf.docinfo:
        del pdf.docinfo[ORIGINAL_ENCODING_INFO_KEY]
    pdf.docinfo[Name.ModDate] = timestamp.strftime("D:%Y%m%d%H%M%S+00'00'")
    for key, value in (
        (Name.Author, source_metadata.author),
        (Name.Subject, source_metadata.subject),
        (Name.Keywords, source_metadata.keywords),
        (Name.CreationDate, source_metadata.creation_date),
    ):
        if value:
            pdf.docinfo[key] = value
        elif key in pdf.docinfo:
            del pdf.docinfo[key]
    if Name.Creator in pdf.docinfo:
        del pdf.docinfo[Name.Creator]


def _remediation_metadata_status(
    pdf: pikepdf.Pdf,
    plan: DocumentPlan | None,
    source_metadata: SourceMetadata | None = None,
) -> dict[str, object]:
    source_metadata = source_metadata or SourceMetadata()
    with pdf.open_metadata(
        set_pikepdf_as_editor=False, update_docinfo=False
    ) as metadata:
        values = {
            "tool": str(metadata.get(f"{REMEDIATION_PREFIX}:tool", "")),
            "version": str(metadata.get(f"{REMEDIATION_PREFIX}:version", "")),
            "remediation_date": str(
                metadata.get(f"{REMEDIATION_PREFIX}:remediationDate", "")
            ),
            "schema_version": str(
                metadata.get(f"{REMEDIATION_PREFIX}:schemaVersion", "")
            ),
            "source_sha256": str(
                metadata.get(f"{REMEDIATION_PREFIX}:sourceSha256", "")
            ),
            "canonical_plan_sha256": str(
                metadata.get(f"{REMEDIATION_PREFIX}:canonicalPlanSha256", "")
            ),
            "xmp_creator_tool": str(metadata.get("xmp:CreatorTool", "")),
            "remediation": str(
                metadata.get(f"{REMEDIATION_PREFIX}:remediation", "")
            ),
            "original_encoding_software": str(
                metadata.get(
                    f"{REMEDIATION_PREFIX}:originalEncodingSoftware", ""
                )
            ),
            "xmp_authors": metadata.get("dc:creator", []) or [],
            "description": str(metadata.get("dc:description", "")),
            "xmp_keywords": str(metadata.get("pdf:Keywords", "")),
            "xmp_creation_date": str(metadata.get("xmp:CreateDate", "")),
        }
    producer = str(pdf.docinfo.get(Name.Producer, ""))
    creator = str(pdf.docinfo.get(Name.Creator, ""))
    info_remediation = str(pdf.docinfo.get(REMEDIATION_INFO_KEY, ""))
    info_original_encoding = str(
        pdf.docinfo.get(ORIGINAL_ENCODING_INFO_KEY, "")
    )
    author = str(pdf.docinfo.get(Name.Author, ""))
    subject = str(pdf.docinfo.get(Name.Subject, ""))
    keywords = str(pdf.docinfo.get(Name.Keywords, ""))
    creation_date = str(pdf.docinfo.get(Name.CreationDate, ""))
    expected_source_hash = plan.source_sha256 if plan else ""
    expected_plan_hash = plan_sha256(plan) if plan else ""
    expected_original_encoding = "; ".join(source_metadata.encoding_software)
    raw_xmp_authors = values["xmp_authors"]
    if not isinstance(raw_xmp_authors, (list, set, tuple)):
        raw_xmp_authors = [raw_xmp_authors]
    values["xmp_authors"] = [
        str(author) for author in raw_xmp_authors if author is not None
    ]
    valid = all(
        [
            values["tool"] == REMEDIATION_TOOL,
            values["version"] == __version__,
            bool(values["remediation_date"]),
            values["schema_version"] == str(SCHEMA_VERSION),
            producer == REMEDIATION_PRODUCER,
            not creator,
            not values["xmp_creator_tool"],
            values["remediation"] == REMEDIATION_SUMMARY,
            values["original_encoding_software"] == expected_original_encoding,
            info_remediation == REMEDIATION_SUMMARY,
            info_original_encoding == expected_original_encoding,
            author == (source_metadata.author or ""),
            subject == (source_metadata.subject or ""),
            keywords == (source_metadata.keywords or ""),
            values["xmp_authors"] == source_metadata.xmp_authors,
            values["description"] == (source_metadata.description or ""),
            values["xmp_keywords"] == (source_metadata.xmp_keywords or ""),
            creation_date == (source_metadata.creation_date or ""),
            values["xmp_creation_date"]
            == (source_metadata.xmp_creation_date or ""),
            not plan or values["source_sha256"] == expected_source_hash,
            not plan or values["canonical_plan_sha256"] == expected_plan_hash,
        ]
    )
    return {
        **values,
        "producer": producer,
        "creator": creator,
        "info_remediation": info_remediation,
        "info_original_encoding_software": info_original_encoding,
        "author": author,
        "subject": subject,
        "keywords": keywords,
        "creation_date": creation_date,
        "valid": valid,
    }


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
        and max(means) <= 0.052
        and max(fractions) <= 0.25
    )
    return {
        "dimensions_match": dimensions_match,
        "sampled_dpi": results,
        "thresholds": {
            "maximum_mean_absolute_channel_difference": 0.052,
            "maximum_sample_fraction_over_16": 0.25,
        },
        "within_tolerance": within_tolerance,
    }


def _actual_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _identity_unicode(value: object) -> str:
    try:
        encoded = bytes(value)
    except (TypeError, ValueError):
        return ""
    if not encoded or len(encoded) % 2:
        return ""
    try:
        return encoded.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _page_mcids(pdf: pikepdf.Pdf) -> tuple[dict[int, dict[int, str]], list[str]]:
    pages: dict[int, dict[int, str]] = {}
    errors: list[str] = []
    for page_number, page in enumerate(pdf.pages, start=1):
        mcids: dict[int, str] = {}
        stack: list[tuple[bool, int | None, bool]] = []
        active_font = ""
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
                stack.append((str(operands[0]) == "/Artifact", None, False))
            elif operator == "BDC":
                properties = operands[1] if len(operands) > 1 else None
                is_artifact = str(operands[0]) == "/Artifact"
                mcid = None
                actual_text = (
                    _actual_text(properties.get("/ActualText"))
                    if isinstance(properties, pikepdf.Dictionary)
                    else ""
                )
                inside_artifact = any(item[0] for item in stack) or is_artifact
                if isinstance(properties, pikepdf.Dictionary) and "/MCID" in properties:
                    mcid = int(properties.MCID)
                    if inside_artifact:
                        errors.append(f"page {page_number} MCID {mcid} is nested in an artifact")
                    if mcid in mcids:
                        errors.append(f"page {page_number} has duplicate MCID {mcid}")
                    mcids[mcid] = actual_text
                elif actual_text and not inside_artifact:
                    owner_mcid = next(
                        (item[1] for item in reversed(stack) if item[1] is not None),
                        None,
                    )
                    if owner_mcid is None:
                        errors.append(
                            f"page {page_number} ActualText span has no owning MCID"
                        )
                    else:
                        mcids[owner_mcid] = mcids.get(owner_mcid, "") + actual_text
                stack.append((is_artifact, mcid, bool(actual_text)))
            elif operator == "Tf" and operands:
                active_font = str(operands[0])
            elif operator in {"Tj", "'", '"'}:
                inside_artifact = any(item[0] for item in stack)
                owner_mcid = next(
                    (item[1] for item in reversed(stack) if item[1] is not None),
                    None,
                )
                if (
                    not inside_artifact
                    and owner_mcid is not None
                    and active_font == "/A11yAnchor"
                    and not any(item[2] for item in stack)
                ):
                    text = _identity_unicode(operands[-1]) if operands else ""
                    mcids[owner_mcid] = mcids.get(owner_mcid, "") + text
            elif operator == "TJ":
                inside_artifact = any(item[0] for item in stack)
                owner_mcid = next(
                    (item[1] for item in reversed(stack) if item[1] is not None),
                    None,
                )
                if (
                    not inside_artifact
                    and owner_mcid is not None
                    and active_font == "/A11yAnchor"
                    and not any(item[2] for item in stack)
                    and operands
                ):
                    text = "".join(
                        _identity_unicode(item)
                        for item in operands[0]
                        if not isinstance(item, (int, float))
                    )
                    mcids[owner_mcid] = mcids.get(owner_mcid, "") + text
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
        element_ids: list[str] = []

        def resolve_mcr(
            content_item: pikepdf.Object | int,
            owner: pikepdf.Object,
            logical_index: int,
        ) -> tuple[str, dict[str, int] | None]:
            if isinstance(content_item, int):
                page_obj = owner.get("/Pg")
                mcid = int(content_item)
            else:
                page_obj = content_item.get("/Pg", owner.get("/Pg"))
                mcid = int(content_item.MCID)
            page_number = page_numbers.get(page_obj.objgen) if page_obj is not None else None
            if page_number is None:
                errors.append(
                    f"structure element {logical_index} MCR has no resolvable page"
                )
                return "", None
            key = (page_number, mcid)
            if key in referenced:
                errors.append(f"page {page_number} MCID {mcid} is referenced more than once")
            referenced.add(key)
            text = page_mcids.get(page_number, {}).get(mcid)
            if text is None:
                errors.append(
                    f"structure element {logical_index} references missing "
                    f"page {page_number} MCID {mcid}"
                )
                text = ""

            struct_parent = int(pdf.pages[page_number - 1].obj.get("/StructParents", -1))
            parent_array = parent_tree_by_key.get(struct_parent)
            if not isinstance(parent_array, pikepdf.Array) or mcid >= len(parent_array):
                errors.append(f"page {page_number} MCID {mcid} is absent from ParentTree")
            elif parent_array[mcid].objgen != owner.objgen:
                errors.append(f"page {page_number} MCID {mcid} ParentTree points elsewhere")
            return text, {"page": page_number, "mcid": mcid}

        def resolve_children(
            owner: pikepdf.Object,
            logical_index: int,
        ) -> tuple[list[str], list[dict[str, int]], list[dict[str, object]]]:
            content_items = owner.get("/K", [])
            if isinstance(content_items, (int, pikepdf.Dictionary)):
                content_items = [content_items]
            chunks: list[str] = []
            mcrs: list[dict[str, int]] = []
            blocks: list[dict[str, object]] = []
            for content_item in content_items:
                if isinstance(content_item, int):
                    text, mcr = resolve_mcr(content_item, owner, logical_index)
                    chunks.append(text)
                    if mcr is not None:
                        mcrs.append(mcr)
                    continue
                if not isinstance(content_item, pikepdf.Dictionary):
                    errors.append(
                        f"structure element {logical_index} has an unsupported child"
                    )
                    continue
                if str(content_item.get("/Type", "")) == "/StructElem":
                    child_id = str(content_item.get("/ID", ""))
                    if child_id:
                        element_ids.append(child_id)
                    parent = content_item.get("/P")
                    if parent is None or parent.objgen != owner.objgen:
                        errors.append(
                            f"structure element {logical_index} has a child with the wrong parent"
                        )
                    child_chunks, child_mcrs, descendants = resolve_children(
                        content_item, logical_index
                    )
                    attributes = content_item.get("/A", {})
                    bbox = (
                        [float(value) for value in attributes.get("/BBox", [])]
                        if isinstance(attributes, pikepdf.Dictionary)
                        else []
                    )
                    blocks.append(
                        {
                            "id": str(content_item.get("/ID", "")),
                            "role": str(content_item.get("/S", "")),
                            "text": "".join(child_chunks),
                            "bbox": bbox,
                            "mcrs": child_mcrs,
                        }
                    )
                    blocks.extend(descendants)
                    chunks.extend(child_chunks)
                    mcrs.extend(child_mcrs)
                    continue
                if "/MCID" not in content_item:
                    errors.append(
                        f"structure element {logical_index} has a non-MCR content child"
                    )
                    continue
                text, mcr = resolve_mcr(content_item, owner, logical_index)
                chunks.append(text)
                if mcr is not None:
                    mcrs.append(mcr)
            return chunks, mcrs, blocks

        def append_record(
            element: pikepdf.Object,
            role: str,
            container_role: str | None = None,
        ) -> None:
            element_index = len(records)
            chunks, mcrs, blocks = resolve_children(element, element_index)
            alt = str(element.get("/Alt", ""))
            text = "".join(chunks)
            semantic_role = (
                str(blocks[0].get("role", ""))
                if role == "/Div" and blocks
                else role
            )
            if role == "/Div" and any(
                block.get("role") != semantic_role for block in blocks
            ):
                errors.append(
                    f"structure element {element_index} has inconsistent block roles"
                )
            if role == "/Figure" and not alt.strip():
                errors.append(f"figure structure element {element_index} has no alternate text")
            if not text and not (semantic_role == "/Figure" and alt):
                errors.append(f"structure element {element_index} is empty")
            records.append(
                {
                    "role": semantic_role,
                    "container_role": container_role or role,
                    "id": str(element.get("/ID", "")),
                    "text": text,
                    "alt_text": alt,
                    "mcrs": mcrs,
                    "blocks": blocks,
                }
            )
            element_id = str(element.get("/ID", ""))
            if element_id:
                element_ids.append(element_id)

        for element in children:
            role = str(element.get("/S", ""))
            if role != "/L":
                append_record(element, role)
                continue

            attributes = element.get("/A", {})
            if (
                not isinstance(attributes, pikepdf.Dictionary)
                or str(attributes.get("/O", "")) != "/List"
            ):
                errors.append("list structure element has no /List attributes")
            list_items = element.get("/K", [])
            if isinstance(list_items, pikepdf.Dictionary):
                list_items = [list_items]
            if not list_items:
                errors.append("list structure element has no list items")
            for list_item in list_items:
                item_index = len(records)
                if (
                    not isinstance(list_item, pikepdf.Dictionary)
                    or str(list_item.get("/S", "")) != "/LI"
                ):
                    errors.append(f"list child {item_index} is not an /LI structure element")
                    continue
                parent = list_item.get("/P")
                if parent is None or parent.objgen != element.objgen:
                    errors.append(f"list item {item_index} has the wrong parent")
                item_children = list_item.get("/K", [])
                if isinstance(item_children, pikepdf.Dictionary):
                    item_children = [item_children]
                item_roles = [
                    str(child.get("/S", ""))
                    for child in item_children
                    if isinstance(child, pikepdf.Dictionary)
                ]
                if "/LBody" not in item_roles:
                    errors.append(f"list item {item_index} has no /LBody")
                if any(item_role not in {"/Lbl", "/LBody"} for item_role in item_roles):
                    errors.append(f"list item {item_index} has an invalid structural child")
                append_record(list_item, "/LI", container_role="/L")

        if element_ids and "/IDTree" not in root:
            errors.append("structure elements have IDs but StructTreeRoot has no IDTree")
        if len(element_ids) != len(set(element_ids)):
            errors.append("structure element IDs are not unique")

        for page_number, mcids in page_mcids.items():
            for mcid in mcids:
                if (page_number, mcid) not in referenced:
                    errors.append(f"page {page_number} MCID {mcid} is not referenced by the structure tree")
    return {"elements": records, "errors": errors}


def compare_structure_to_plan(serialized: dict[str, object], plan: DocumentPlan) -> list[str]:
    errors: list[str] = list(serialized.get("errors", []))
    actual = list(serialized.get("elements", []))
    expected = [element for page in plan.pages for element in page.elements]
    cursor = 0
    for index, element in enumerate(expected):
        expected_role = EXPECTED_PDF_ROLES[element.role]
        region_count = 1
        if element.role == ElementRole.P and len(element.visible_fragments) > 1:
            groups = fragment_region_groups(element.visible_fragments)
            if len(groups) > 1:
                region_count = len(groups)
                if (
                    cursor < len(actual)
                    and actual[cursor]["role"] == expected_role
                    and actual[cursor]["text"] == element.semantic_text
                ):
                    region_count = 1
        records = actual[cursor : cursor + region_count]
        cursor += region_count
        if len(records) != region_count:
            errors.append(
                f"element {index} has {len(records)} structure regions; "
                f"expected {region_count}"
            )
            continue
        for region_index, record in enumerate(records):
            if record["role"] != expected_role:
                errors.append(
                    f"element {index} region {region_index} role "
                    f"{record['role']} != {expected_role}"
                )
        expected_text = "" if element.role == ElementRole.FIGURE else element.semantic_text
        if "".join(str(record["text"]) for record in records) != expected_text:
            errors.append(f"element {index} exact text does not match canonical plan")
        if (
            element.role == ElementRole.FIGURE
            and records[0]["alt_text"] != (element.alt_text or "")
        ):
            errors.append(f"figure {index} alternate text does not match canonical plan")
    if cursor != len(actual):
        errors.append(
            f"structure has {len(actual)} regions; canonical plan accounts for {cursor}"
        )
    return errors


def block_plan_errors(plan: DocumentPlan) -> list[str]:
    errors: list[str] = []
    for page in plan.pages:
        blocks = [
            fragment
            for element in page.elements
            for fragment in element.visible_fragments
        ]
        block_ids = [fragment.id for fragment in blocks]
        if len(block_ids) != len(set(block_ids)):
            errors.append(f"page {page.page_number}: duplicate visual block identifiers")
        if page.block_order != block_ids:
            errors.append(
                f"page {page.page_number}: flow order does not match semantic block order"
            )
        for fragment in blocks:
            if fragment.bbox is None:
                errors.append(f"{fragment.id}: visual block has no bounding box")
                continue
            left, top, right, bottom = fragment.bbox
            if not (
                0 <= left < right <= 1000
                and 0 <= top < bottom <= 1000
            ):
                errors.append(f"{fragment.id}: visual block box is outside normalized bounds")
            if fragment.alignment_coverage is None or fragment.geometry_source is None:
                errors.append(f"{fragment.id}: visual block was not locally aligned")
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
        element.semantic_text
        for page in plan.pages
        for element in page.elements
        if element.role != ElementRole.FIGURE
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
    source_metadata: SourceMetadata | None = None,
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
    plan_block_errors = block_plan_errors(plan) if plan else []
    with pikepdf.Pdf.open(output) as pdf:
        has_structure_tree = "/StructTreeRoot" in pdf.Root
        marked = bool(pdf.Root.get("/MarkInfo", {}).get("/Marked", False))
        tagged_pages = sum("/StructParents" in page.obj for page in pdf.pages)
        language = str(pdf.Root.get("/Lang", ""))
        title = str(pdf.docinfo.get("/Title", ""))
        with pdf.open_outline() as outline:
            bookmark_count = len(outline.root)
        with pdf.open_metadata(
            set_pikepdf_as_editor=False, update_docinfo=False
        ) as metadata:
            declares_pdfua = metadata.get("pdfuaid:part") == "1"
        page_count = len(pdf.pages)
        page_labels_present = "/PageLabels" in pdf.Root
        remediation_metadata = _remediation_metadata_status(
            pdf, plan, source_metadata
        )
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
        "remediation_metadata": remediation_metadata,
        "remediation_metadata_valid": remediation_metadata["valid"],
        "transformation_errors": plan_transformation_errors,
        "transformations_valid": not plan_transformation_errors,
        "block_plan_errors": plan_block_errors,
        "block_plan_valid": not plan_block_errors,
        "source_visual_fidelity": source_fidelity,
        "source_visual_fidelity_ok": (
            source_fidelity["within_tolerance"] if source_fidelity else True
        ),
        **extraction,
    }


def add_pdfua_declaration(
    draft: Path,
    candidate: Path,
    plan: DocumentPlan | None = None,
    remediated_at: datetime | None = None,
    source_metadata: SourceMetadata | None = None,
) -> None:
    if candidate.exists():
        candidate.unlink()
    with pikepdf.Pdf.open(draft) as pdf:
        _apply_remediation_metadata(pdf, plan, remediated_at, source_metadata)
        pdf.save(candidate, min_version="1.4", linearize=True)


def release_pdfua(
    source: Path,
    draft: Path,
    output: Path,
    plan: DocumentPlan,
    reference_source: Path | None = None,
    source_metadata: SourceMetadata | None = None,
) -> dict[str, object]:
    if source_metadata is None and reference_source is not None:
        source_metadata = read_source_metadata(reference_source)
    candidate = output.with_suffix(".candidate.pdf")
    add_pdfua_declaration(
        draft, candidate, plan, source_metadata=source_metadata
    )
    report = validate_output(
        source,
        candidate,
        plan,
        reference_source=reference_source,
        source_metadata=source_metadata,
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
            report["block_plan_valid"],
            report["extraction_compatible"],
            report["declares_pdfua"],
            report["remediation_metadata_valid"],
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
