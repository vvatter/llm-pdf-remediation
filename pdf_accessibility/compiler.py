from __future__ import annotations

import os
import textwrap
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pikepdf
import pymupdf
from fontTools.ttLib import TTFont
from pikepdf import Array, Dictionary, Name, OutlineItem, Stream, String

from .models import DocumentPlan, ElementRole, PageElement, exact_text_tokens


ROLE_NAMES = {
    ElementRole.DOCUMENT_TITLE: Name.H1,
    ElementRole.H1: Name.H1,
    ElementRole.H2: Name.H2,
    ElementRole.H3: Name.H3,
    ElementRole.P: Name.P,
    ElementRole.FIGURE: Name.Figure,
}


@dataclass(frozen=True)
class AnchorChunk:
    element: PageElement
    text: str
    token_text: str
    mcid: int
    offset: int


@dataclass(frozen=True)
class AnchorFont:
    resource: pikepdf.Object
    supported_codepoints: frozenset[int]
    advances: dict[int, int]


@dataclass(frozen=True)
class WordPlacement:
    text: str
    bbox: tuple[float, float, float, float]


def _as_contents_array(contents: pikepdf.Object | None) -> list[pikepdf.Object]:
    if contents is None:
        return []
    if isinstance(contents, pikepdf.Array):
        return list(contents)
    return [contents]


def _find_anchor_font() -> Path:
    configured = os.getenv("A11Y_FONT_PATH")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).parent / "fonts" / "NotoSans-Regular.ttf",
        Path("/usr/local/texlive/2024/texmf-dist/fonts/truetype/google/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No open TrueType anchor font found; set A11Y_FONT_PATH to Noto Sans or DejaVu Sans"
    )


def _scale_metric(value: int, units_per_em: int) -> int:
    return round(value * 1000 / units_per_em)


def _to_unicode_cmap(codepoints: set[int]) -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /A11yUnicode def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    ordered = sorted(codepoints)
    for start in range(0, len(ordered), 100):
        group = ordered[start : start + 100]
        lines.append(f"{len(group)} beginbfchar")
        lines.extend(f"<{codepoint:04X}> <{codepoint:04X}>" for codepoint in group)
        lines.append("endbfchar")
    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
        ]
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def _make_anchor_font(pdf: pikepdf.Pdf, plan: DocumentPlan) -> AnchorFont:
    font_path = _find_anchor_font()
    font_bytes = font_path.read_bytes()
    font = TTFont(font_path, lazy=False)
    cmap = font.getBestCmap()
    requested = {
        ord(character)
        for page in plan.pages
        for element in page.elements
        for character in (
            element.semantic_text
        ) or ""
        if ord(character) <= 0xFFFF
    }
    fallback = ord("?")
    supported = {codepoint for codepoint in requested | {fallback} if codepoint in cmap}
    units_per_em = font["head"].unitsPerEm

    max_codepoint = max(supported)
    cid_to_gid = bytearray((max_codepoint + 1) * 2)
    widths = Array()
    for codepoint in sorted(supported):
        glyph_name = cmap[codepoint]
        glyph_id = font.getGlyphID(glyph_name)
        cid_to_gid[codepoint * 2 : codepoint * 2 + 2] = glyph_id.to_bytes(2, "big")
        advance, _ = font["hmtx"].metrics[glyph_name]
        widths.extend([codepoint, Array([_scale_metric(advance, units_per_em)])])

    head = font["head"]
    hhea = font["hhea"]
    os2 = font["OS/2"]
    post = font["post"]
    embedded_font = Stream(pdf, font_bytes)
    embedded_font[Name.Length1] = len(font_bytes)
    descriptor = pdf.make_indirect(
        Dictionary(
            Type=Name.FontDescriptor,
            FontName=Name("/NotoSans-Regular"),
            Flags=32,
            FontBBox=Array(
                [
                    _scale_metric(head.xMin, units_per_em),
                    _scale_metric(head.yMin, units_per_em),
                    _scale_metric(head.xMax, units_per_em),
                    _scale_metric(head.yMax, units_per_em),
                ]
            ),
            ItalicAngle=post.italicAngle,
            Ascent=_scale_metric(hhea.ascent, units_per_em),
            Descent=_scale_metric(hhea.descent, units_per_em),
            CapHeight=_scale_metric(getattr(os2, "sCapHeight", hhea.ascent), units_per_em),
            StemV=80,
            FontFile2=embedded_font,
        )
    )
    descendant = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name("/CIDFontType2"),
            BaseFont=Name("/NotoSans-Regular"),
            CIDSystemInfo=Dictionary(
                Registry=String("Adobe"), Ordering=String("Identity"), Supplement=0
            ),
            FontDescriptor=descriptor,
            DW=1000,
            W=widths,
            CIDToGIDMap=Stream(pdf, bytes(cid_to_gid)),
        )
    )
    resource = pdf.make_indirect(
        Dictionary(
            Type=Name.Font,
            Subtype=Name.Type0,
            BaseFont=Name("/NotoSans-Regular"),
            Encoding=Name("/Identity-H"),
            DescendantFonts=Array([descendant]),
            ToUnicode=Stream(pdf, _to_unicode_cmap(supported)),
        )
    )
    advances = {
        codepoint: _scale_metric(font["hmtx"].metrics[cmap[codepoint]][0], units_per_em)
        for codepoint in supported
    }
    font.close()
    return AnchorFont(
        resource=resource,
        supported_codepoints=frozenset(supported),
        advances=advances,
    )


def _ensure_anchor_font(page: pikepdf.Page, anchor_font: AnchorFont) -> None:
    inherited = page.obj.get(Name.Resources, Dictionary())
    resources = Dictionary()
    for key, value in inherited.items():
        resources[key] = value
    inherited_fonts = resources.get(Name.Font, Dictionary())
    fonts = Dictionary()
    for key, value in inherited_fonts.items():
        fonts[key] = value
    fonts[Name("/A11yAnchor")] = anchor_font.resource
    resources[Name.Font] = fonts
    page.obj[Name.Resources] = resources


def _pdf_xy(element: PageElement, width: float, height: float) -> tuple[float, float]:
    left, top, right, bottom = element.bbox
    x = min(max(left / 1000 * width, 0.0), width)
    y = min(max(height - (top / 1000 * height), 0.0), height)
    return x, y


def _unicode_lines(
    text: str, supported_codepoints: frozenset[int], width: int = 80
) -> list[bytes]:
    flattened = " ".join(text.split())
    wrapped = textwrap.wrap(
        flattened,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    return [
        "".join(
            character
            if ord(character) <= 0xFFFF and ord(character) in supported_codepoints
            else "?"
            for character in line
        ).encode("utf-16-be")
        for line in wrapped
    ]


def _token_key(token: str) -> str:
    decomposed = unicodedata.normalize("NFKD", token).lower()
    letters_and_numbers = "".join(
        character for character in decomposed if character.isalnum()
    )
    return letters_and_numbers or token


def _subdivide_bbox(
    bbox: tuple[float, float, float, float], index: int, count: int
) -> tuple[float, float, float, float]:
    if count <= 1:
        return bbox
    x0, y0, x1, y1 = bbox
    width = max(x1 - x0, 1.0)
    return (
        x0 + width * index / count,
        y0,
        x0 + width * (index + 1) / count,
        y1,
    )


def _allocate_replacement_boxes(
    source_boxes: list[tuple[float, float, float, float]], count: int
) -> list[tuple[float, float, float, float]]:
    if not source_boxes:
        return []
    assignments = [
        min(index * len(source_boxes) // count, len(source_boxes) - 1)
        for index in range(count)
    ]
    totals = {
        source_index: assignments.count(source_index)
        for source_index in set(assignments)
    }
    seen: dict[int, int] = {}
    allocated = []
    for source_index in assignments:
        offset = seen.get(source_index, 0)
        allocated.append(
            _subdivide_bbox(source_boxes[source_index], offset, totals[source_index])
        )
        seen[source_index] = offset + 1
    return allocated


def _align_corrected_words(
    chunks: list[AnchorChunk],
    ocr_words: list[tuple[float, float, float, float, str]],
) -> dict[int, list[WordPlacement]]:
    corrected = [
        (chunk.token_text, chunk.mcid)
        for chunk in chunks
        if chunk.token_text
    ]
    ocr_keys = [_token_key(word[4]) for word in ocr_words]
    corrected_keys = [_token_key(token) for token, _ in corrected]
    matcher = SequenceMatcher(None, ocr_keys, corrected_keys, autojunk=False)
    placements: list[tuple[float, float, float, float] | None] = [None] * len(corrected)

    for tag, ocr_start, ocr_end, corrected_start, corrected_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(corrected_end - corrected_start):
                placements[corrected_start + offset] = ocr_words[ocr_start + offset][:4]
            continue
        if tag == "delete":
            continue

        replacement_count = corrected_end - corrected_start
        source_boxes = [word[:4] for word in ocr_words[ocr_start:ocr_end]]
        if not source_boxes:
            neighbor = None
            if ocr_start:
                neighbor = ocr_words[ocr_start - 1][:4]
            elif ocr_start < len(ocr_words):
                neighbor = ocr_words[ocr_start][:4]
            if neighbor:
                source_boxes = [neighbor]
        for offset, bbox in enumerate(
            _allocate_replacement_boxes(source_boxes, replacement_count)
        ):
            placements[corrected_start + offset] = bbox

    fallback = ocr_words[0][:4] if ocr_words else (36.0, 36.0, 72.0, 48.0)
    by_mcid: dict[int, list[WordPlacement]] = {chunk.mcid: [] for chunk in chunks}
    for index, ((token, mcid), bbox) in enumerate(zip(corrected, placements, strict=True)):
        if bbox is None:
            previous = next(
                (placements[prior] for prior in range(index - 1, -1, -1) if placements[prior]),
                None,
            )
            following = next(
                (
                    placements[later]
                    for later in range(index + 1, len(placements))
                    if placements[later]
                ),
                None,
            )
            bbox = previous or following or fallback
        by_mcid[mcid].append(WordPlacement(text=token, bbox=bbox))
    return by_mcid


def _extract_ocr_words(source: Path) -> list[list[tuple[float, float, float, float, str]]]:
    pages: list[list[tuple[float, float, float, float, str]]] = []
    with pymupdf.open(source) as document:
        for page in document:
            pages.append(
                [
                    (float(x0), float(y0), float(x1), float(y1), str(word))
                    for x0, y0, x1, y1, word, *_ in page.get_text("words", sort=False)
                ]
            )
    return pages


def _alignment_quality(
    chunks: list[AnchorChunk],
    words: list[tuple[float, float, float, float, str]],
) -> float:
    corrected_keys = [
        _token_key(chunk.token_text) for chunk in chunks if chunk.token_text
    ]
    word_keys = [_token_key(word[4]) for word in words]
    if not corrected_keys:
        return 1.0
    if not word_keys:
        return 0.0
    matcher = SequenceMatcher(None, word_keys, corrected_keys, autojunk=False)
    matches = sum(block.size for block in matcher.get_matching_blocks())
    corrected_coverage = matches / len(corrected_keys)
    source_precision = matches / len(word_keys)
    return corrected_coverage * 0.75 + source_precision * 0.25


def _select_geometry_words(
    plan: DocumentPlan,
    base_words: list[list[tuple[float, float, float, float, str]]],
    candidate_words: list[list[tuple[float, float, float, float, str]]] | None,
    primary_label: str = "ocr",
    candidate_label: str = "native",
) -> tuple[
    list[list[tuple[float, float, float, float, str]]],
    list[str],
]:
    selected: list[list[tuple[float, float, float, float, str]]] = []
    sources: list[str] = []
    plan_by_page = {page.page_number: page for page in plan.pages}
    for page_index, primary in enumerate(base_words):
        page_plan = plan_by_page.get(page_index + 1)
        alternative = (
            candidate_words[page_index]
            if candidate_words and page_index < len(candidate_words)
            else None
        )
        if page_plan is None or alternative is None:
            selected.append(primary)
            sources.append(primary_label)
            continue
        chunks = [
            chunk
            for element_chunks in _page_anchor_chunks(page_plan.elements)
            for chunk in element_chunks
        ]
        primary_quality = _alignment_quality(chunks, primary)
        alternative_quality = _alignment_quality(chunks, alternative)
        if alternative_quality > primary_quality:
            selected.append(alternative)
            sources.append(candidate_label)
        else:
            selected.append(primary)
            sources.append(primary_label)
    return selected, sources


def _suppress_ocr_text(page: pikepdf.Page) -> bool:
    suppressed = False
    for name, xobject in page.Resources.get(Name.XObject, {}).items():
        if str(name).startswith("/OCR-") and str(xobject.get(Name.Subtype)) == "/Form":
            xobject.write(b"")
            suppressed = True
    return suppressed


def _page_anchor_chunks(elements: list[PageElement]) -> list[list[AnchorChunk]]:
    by_element: list[list[AnchorChunk]] = []
    next_mcid = 0
    for element in elements:
        actual_text = element.semantic_text
        element_chunks: list[AnchorChunk] = []
        tokens = exact_text_tokens(actual_text)
        for offset, token in enumerate(tokens):
            element_chunks.append(
                AnchorChunk(
                    element=element,
                    text=actual_text[token.start : token.actual_end],
                    token_text=token.text,
                    mcid=next_mcid,
                    offset=offset,
                )
            )
            next_mcid += 1
        by_element.append(element_chunks)
    return by_element


def _marked_content_start(chunk: AnchorChunk) -> str:
    actual_text = b"\xfe\xff" + chunk.text.encode("utf-16-be")
    return (
        f"/Span <</MCID {chunk.mcid} "
        f"/ActualText <{actual_text.hex().upper()}>>> BDC"
    )
def _anchor_stream(
    chunks: list[AnchorChunk],
    width: float,
    height: float,
    anchor_font: AnchorFont,
    placements: dict[int, list[WordPlacement]] | None = None,
) -> bytes:
    commands: list[str] = []
    for chunk in chunks:
        placed_words = placements.get(chunk.mcid, []) if placements else []
        if placed_words:
            commands.append(_marked_content_start(chunk))
            for word in placed_words:
                x0, y0, x1, y1 = word.bbox
                font_size = max(1.0, min(72.0, (y1 - y0) * 0.82))
                encoded = _unicode_lines(
                    word.text, anchor_font.supported_codepoints, width=10_000
                )[0]
                advance = sum(
                    anchor_font.advances.get(
                        ord(character), anchor_font.advances.get(ord("?"), 600)
                    )
                    for character in word.text
                )
                natural_width = max(advance * font_size / 1000, 0.01)
                horizontal_scale = max(10.0, min(1000.0, (x1 - x0) / natural_width * 100))
                baseline = height - y1 + font_size * 0.18
                commands.append(
                    f"BT /A11yAnchor {font_size:.3f} Tf 3 Tr {horizontal_scale:.3f} Tz "
                    f"1 0 0 1 {x0:.3f} {baseline:.3f} Tm "
                    f"<{encoded.hex().upper()}> Tj ET"
                )
            commands.append("EMC")
            continue

        x, y = _pdf_xy(chunk.element, width, height)
        y = max(0.0, y - chunk.offset)
        commands.append(_marked_content_start(chunk))
        commands.append(f"BT /A11yAnchor 1 Tf 3 Tr 1 TL 1 0 0 1 {x:.3f} {y:.3f} Tm")
        for line_index, line in enumerate(
            _unicode_lines(chunk.text, anchor_font.supported_codepoints)
        ):
            if line_index:
                commands.append("T*")
            commands.append(f"<{line.hex().upper()}> Tj")
        commands.extend(["ET", "EMC"])
    return ("\n".join(commands) + "\n").encode("ascii")


def _make_role_element(
    pdf: pikepdf.Pdf,
    element: PageElement,
    page_obj: pikepdf.Object,
    parent: pikepdf.Object,
    chunks: list[AnchorChunk],
) -> tuple[pikepdf.Object, list[pikepdf.Object]]:
    role = ROLE_NAMES[element.role]
    role_element = pdf.make_indirect(
        Dictionary(Type=Name.StructElem, S=role, P=parent, Pg=page_obj)
    )
    content_items = Array(
        [
            Dictionary(Type=Name("/MCR"), Pg=page_obj, MCID=chunk.mcid)
            for chunk in chunks
        ]
    )
    role_element[Name.K] = content_items
    if element.role == ElementRole.FIGURE:
        role_element[Name.Alt] = String(
            element.alt_text or "Historical document image"
        )
    return role_element, [role_element] * len(chunks)


def compile_tagged_pdf(
    source: Path,
    output: Path,
    plan: DocumentPlan,
    geometry_source: Path | None = None,
    declare_pdfua: bool = False,
) -> list[str]:
    base_words = _extract_ocr_words(source)
    candidate_words = (
        _extract_ocr_words(geometry_source)
        if geometry_source and geometry_source.resolve() != source.resolve()
        else None
    )
    geometry_words_by_page, geometry_sources = _select_geometry_words(
        plan,
        base_words,
        candidate_words,
        primary_label=(
            "native"
            if geometry_source and geometry_source.resolve() == source.resolve()
            else "ocr"
        ),
        candidate_label="native",
    )
    for page_plan in plan.pages:
        page_index = page_plan.page_number - 1
        if page_index >= len(geometry_words_by_page):
            continue
        chunks = [
            chunk
            for element_chunks in _page_anchor_chunks(page_plan.elements)
            for chunk in element_chunks
        ]
        coverage = _alignment_quality(chunks, geometry_words_by_page[page_index])
        for element in page_plan.elements:
            element.evidence.alignment_coverage = coverage
    with pikepdf.Pdf.open(source) as pdf:
        anchor_font = _make_anchor_font(pdf, plan)
        structure_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        document = pdf.make_indirect(
            Dictionary(Type=Name.StructElem, S=Name.Document, P=structure_root, K=Array())
        )
        structure_root[Name.K] = document

        parent_tree_entries = Array()
        plan_by_page = {page.page_number: page for page in plan.pages}

        for page_index, page in enumerate(pdf.pages):
            page_plan = plan_by_page.get(page_index + 1)
            if page_plan is None:
                continue

            original_contents = _as_contents_array(page.obj.get(Name.Contents))
            artifact_start = Stream(pdf, b"q\n/Artifact BMC\n")
            artifact_end = Stream(pdf, b"EMC\nQ\n")
            width = float(page.mediabox[2]) - float(page.mediabox[0])
            height = float(page.mediabox[3]) - float(page.mediabox[1])
            _ensure_anchor_font(page, anchor_font)
            chunks_by_element = _page_anchor_chunks(page_plan.elements)
            all_chunks = [chunk for chunks in chunks_by_element for chunk in chunks]
            _suppress_ocr_text(page)
            placements = (
                _align_corrected_words(all_chunks, geometry_words_by_page[page_index])
                if geometry_words_by_page[page_index]
                else None
            )
            anchors = Stream(
                pdf,
                _anchor_stream(all_chunks, width, height, anchor_font, placements),
            )
            page.obj[Name.Contents] = Array(
                [artifact_start, *original_contents, artifact_end, anchors]
            )
            page.obj[Name.StructParents] = page_index
            page.obj[Name.Tabs] = Name.S

            mcid_parents = Array()
            for element, chunks in zip(page_plan.elements, chunks_by_element, strict=True):
                role_element, span_parents = _make_role_element(
                    pdf, element, page.obj, document, chunks
                )
                document[Name.K].append(role_element)
                mcid_parents.extend(span_parents)

            parent_tree_entries.extend([page_index, mcid_parents])

        parent_tree = pdf.make_indirect(Dictionary(Nums=parent_tree_entries))
        structure_root[Name.ParentTree] = parent_tree
        structure_root[Name.ParentTreeNextKey] = len(pdf.pages)

        pdf.Root[Name.StructTreeRoot] = structure_root
        pdf.Root[Name.MarkInfo] = Dictionary(Marked=True)
        pdf.Root[Name.Lang] = String(plan.language)
        pdf.Root[Name.ViewerPreferences] = Dictionary(DisplayDocTitle=True)
        pdf.docinfo[Name.Title] = String(plan.title)
        with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
            metadata["dc:title"] = plan.title
            metadata["dc:language"] = [plan.language]
            if declare_pdfua:
                metadata["pdfuaid:part"] = "1"
            elif "pdfuaid:part" in metadata:
                del metadata["pdfuaid:part"]

        with pdf.open_outline() as outline:
            outline.root.clear()
            for page_index, page_plan in enumerate(plan.pages):
                for element in page_plan.elements:
                    if element.role in {
                        ElementRole.DOCUMENT_TITLE,
                        ElementRole.H1,
                        ElementRole.H2,
                    }:
                        outline.root.append(
                            OutlineItem(element.accessible_text[:240], page_index)
                        )

        output.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(output, min_version="1.4", linearize=True)
    return geometry_sources
