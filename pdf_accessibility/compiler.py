from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import groupby
from pathlib import Path

import pikepdf
import pymupdf
from fontTools.ttLib import TTFont
from pikepdf import Array, Dictionary, Name, OutlineItem, Stream, String

from .models import (
    DocumentPlan,
    ElementRole,
    PageElement,
    TextFragment,
    exact_text_tokens,
    fragment_region_groups,
)


ROLE_NAMES = {
    ElementRole.DOCUMENT_TITLE: Name.H1,
    ElementRole.H1: Name.H1,
    ElementRole.H2: Name.H2,
    ElementRole.H3: Name.H3,
    ElementRole.P: Name.P,
    ElementRole.LI: Name.LBody,
    ElementRole.FIGURE: Name.Figure,
}


@dataclass(frozen=True)
class AnchorChunk:
    element: PageElement
    text: str
    token_text: str
    mcid: int
    offset: int
    text_start: int = 0
    text_end: int = 0
    formula_index: int | None = None
    formula_alt: str | None = None


@dataclass(frozen=True)
class AnchorFont:
    resource: pikepdf.Object
    supported_codepoints: frozenset[int]
    advances: dict[int, int]


@dataclass(frozen=True)
class WordPlacement:
    text: str
    bbox: tuple[float, float, float, float]
    line_key: tuple[int, int] | None = None


@dataclass(frozen=True)
class LinePlacement:
    text: str
    bbox: tuple[float, float, float, float]
    chunks: list[AnchorChunk]


@dataclass(frozen=True)
class StructureRegion:
    element: PageElement
    chunks: list[AnchorChunk]
    fragments: list[TextFragment]
    lines: list[LinePlacement]
    segments: list[StructureSegment]
    fragment_anchored: bool = False


@dataclass(frozen=True)
class StructureSegment:
    element: PageElement
    chunks: list[AnchorChunk]
    fragments: list[TextFragment]
    mcid: int
    line_index: int
    formula_index: int | None = None
    formula_alt: str | None = None


@dataclass(frozen=True)
class RegionLineCandidate:
    element: PageElement
    chunks: list[AnchorChunk]
    fragments: list[TextFragment]
    logical_lines: list[LinePlacement]
    fragment_lines: list[LinePlacement]


def _as_contents_array(contents: pikepdf.Object | None) -> list[pikepdf.Object]:
    if contents is None:
        return []
    if isinstance(contents, pikepdf.Array):
        return list(contents)
    return [contents]


def _source_content_without_marking(
    pdf: pikepdf.Pdf, page: pikepdf.Page
) -> list[pikepdf.Object]:
    """Preserve source painting operators but discard obsolete marking wrappers."""
    original = _as_contents_array(page.obj.get(Name.Contents))
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except (pikepdf.PdfError, ValueError):
        return original
    filtered = [
        instruction
        for instruction in instructions
        if str(getattr(instruction, "operator", "")) not in {"BMC", "BDC", "EMC"}
    ]
    if len(filtered) == len(instructions):
        return original
    streams = [item for item in original if isinstance(item, pikepdf.Stream)]
    if not streams:
        return original
    streams[0].write(pikepdf.unparse_content_stream(filtered))
    for stream in streams[1:]:
        stream.write(b"")
    return original


def _strip_form_xobject_marking(
    resources: pikepdf.Object | None,
    visited: set[tuple[int, int]],
) -> None:
    """Remove obsolete marked-content wrappers from nested visible Form XObjects."""
    if resources is None:
        return
    for xobject in resources.get(Name.XObject, {}).values():
        if str(xobject.get(Name.Subtype)) != "/Form":
            continue
        identity = xobject.objgen
        if identity in visited:
            continue
        visited.add(identity)
        try:
            instructions = list(pikepdf.parse_content_stream(xobject))
        except (pikepdf.PdfError, ValueError):
            instructions = []
        filtered = [
            instruction
            for instruction in instructions
            if str(getattr(instruction, "operator", ""))
            not in {"BMC", "BDC", "EMC"}
        ]
        if len(filtered) != len(instructions):
            xobject.write(pikepdf.unparse_content_stream(filtered))
        _strip_form_xobject_marking(xobject.get(Name.Resources), visited)


def _link_description(annotation: pikepdf.Object) -> str:
    existing = str(annotation.get(Name.Contents, "")).strip()
    if existing:
        return existing
    action = annotation.get(Name.A, {})
    uri = str(action.get(Name.URI, "")).strip() if isinstance(action, pikepdf.Dictionary) else ""
    if uri:
        return f"Link to {uri}"
    if annotation.get(Name.Dest) is not None:
        return "Internal document link"
    return "Link"


def _anchor_font_candidates() -> list[Path]:
    configured = os.getenv("A11Y_FONT_PATH")
    texlive_noto_fonts = sorted(
        Path("/usr/local/texlive").glob(
            "*/texmf-dist/fonts/truetype/google/noto/NotoSans-Regular.ttf"
        ),
        reverse=True,
    )
    texlive_dejavu_fonts = sorted(
        Path("/usr/local/texlive").glob(
            "*/texmf-dist/fonts/truetype/public/dejavu/DejaVuSans.ttf"
        ),
        reverse=True,
    )
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(__file__).parent / "fonts" / "NotoSans-Regular.ttf",
        *texlive_noto_fonts,
        *texlive_dejavu_fonts,
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved not in seen:
            found.append(candidate)
            seen.add(resolved)
    return found


def _find_anchor_font(required_codepoints: set[int] | None = None) -> Path:
    candidates = _anchor_font_candidates()
    if candidates and not required_codepoints:
        return candidates[0]
    best: tuple[int, int, Path] | None = None
    for index, candidate in enumerate(candidates):
        font = TTFont(candidate, lazy=True)
        try:
            cmap = font.getBestCmap() or {}
            coverage = len(required_codepoints & cmap.keys())
        finally:
            font.close()
        score = (coverage, -index, candidate)
        if best is None or score[:2] > best[:2]:
            best = score
    if best is not None:
        return best[2]
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
    requested = {
        ord(character)
        for page in plan.pages
        for element in page.elements
        for character in element.extraction_text
        if ord(character) <= 0xFFFF
    }
    fallback = ord("?")
    font_path = _find_anchor_font(requested | {fallback})
    font_bytes = font_path.read_bytes()
    font = TTFont(font_path, lazy=False)
    cmap = font.getBestCmap()
    font_name = re.sub(r"[^A-Za-z0-9_.+-]", "-", font_path.stem)
    control_codepoints = requested & {9, 10, 13}
    supported = {
        codepoint for codepoint in requested | {fallback} if codepoint in cmap
    } | control_codepoints
    units_per_em = font["head"].unitsPerEm

    max_codepoint = max(supported)
    cid_to_gid = bytearray((max_codepoint + 1) * 2)
    widths = Array()
    for codepoint in sorted(supported):
        glyph_name = cmap.get(codepoint)
        glyph_id = font.getGlyphID(glyph_name) if glyph_name else 0
        cid_to_gid[codepoint * 2 : codepoint * 2 + 2] = glyph_id.to_bytes(2, "big")
        advance = font["hmtx"].metrics[glyph_name][0] if glyph_name else 0
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
            FontName=Name(f"/{font_name}"),
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
            BaseFont=Name(f"/{font_name}"),
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
            BaseFont=Name(f"/{font_name}"),
            Encoding=Name("/Identity-H"),
            DescendantFonts=Array([descendant]),
            ToUnicode=Stream(pdf, _to_unicode_cmap(supported)),
        )
    )
    advances = {
        codepoint: (
            _scale_metric(font["hmtx"].metrics[cmap[codepoint]][0], units_per_em)
            if codepoint in cmap
            else 0
        )
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


def _direct_unicode_text(text: str, supported_codepoints: frozenset[int]) -> str:
    return "".join(
        character
        if ord(character) <= 0xFFFF and ord(character) in supported_codepoints
        else " "
        if character.isspace()
        else "?"
        for character in text
    )


def _token_key(token: str) -> str:
    decomposed = unicodedata.normalize("NFKD", token).lower()
    letters_and_numbers = "".join(
        character for character in decomposed if character.isalnum()
    )
    return letters_and_numbers or token


def _word_bbox(word: tuple) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in word[:4])


def _word_text(word: tuple) -> str:
    return str(word[4])


def _word_line_key(word: tuple) -> tuple[int, int] | None:
    if len(word) < 7:
        return None
    return int(word[5]), int(word[6])


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


def _allocate_replacement_placements(
    source_words: list[tuple], count: int
) -> list[tuple[tuple[float, float, float, float], tuple[int, int] | None]]:
    if not source_words:
        return []
    assignments = [
        min(index * len(source_words) // count, len(source_words) - 1)
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
            (
                _subdivide_bbox(
                    _word_bbox(source_words[source_index]),
                    offset,
                    totals[source_index],
                ),
                _word_line_key(source_words[source_index]),
            )
        )
        seen[source_index] = offset + 1
    return allocated


def _align_corrected_words(
    chunks: list[AnchorChunk],
    ocr_words: list[tuple],
    fallback_bbox: tuple[float, float, float, float] | None = None,
) -> dict[int, list[WordPlacement]]:
    corrected = [
        (chunk.token_text, chunk.mcid)
        for chunk in chunks
        if chunk.token_text
    ]
    ocr_keys = [_token_key(_word_text(word)) for word in ocr_words]
    corrected_keys = [_token_key(token) for token, _ in corrected]
    matcher = SequenceMatcher(None, ocr_keys, corrected_keys, autojunk=False)
    placements: list[
        tuple[tuple[float, float, float, float], tuple[int, int] | None] | None
    ] = [None] * len(corrected)

    for tag, ocr_start, ocr_end, corrected_start, corrected_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(corrected_end - corrected_start):
                word = ocr_words[ocr_start + offset]
                placements[corrected_start + offset] = (
                    _word_bbox(word),
                    _word_line_key(word),
                )
            continue
        if tag == "delete":
            continue

        replacement_count = corrected_end - corrected_start
        source_words = list(ocr_words[ocr_start:ocr_end])
        if not source_words:
            neighbor = None
            if ocr_start:
                neighbor = ocr_words[ocr_start - 1]
            elif ocr_start < len(ocr_words):
                neighbor = ocr_words[ocr_start]
            if neighbor:
                source_words = [neighbor]
        for offset, placement in enumerate(
            _allocate_replacement_placements(source_words, replacement_count)
        ):
            placements[corrected_start + offset] = placement

    fallback = (
        _word_bbox(ocr_words[0])
        if ocr_words
        else fallback_bbox or (36.0, 36.0, 72.0, 48.0)
    )
    by_mcid: dict[int, list[WordPlacement]] = {chunk.mcid: [] for chunk in chunks}
    for index, ((token, mcid), placement) in enumerate(
        zip(corrected, placements, strict=True)
    ):
        if placement is None:
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
            placement = previous or following
            if placement is None and fallback_bbox is not None:
                placement = (
                    _subdivide_bbox(fallback_bbox, index, len(corrected)),
                    None,
                )
            placement = placement or (fallback, None)
        bbox, line_key = placement
        by_mcid[mcid].append(
            WordPlacement(text=token, bbox=bbox, line_key=line_key)
        )
    return by_mcid


def _fragment_bbox_points(
    fragment: TextFragment,
    element: PageElement,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = fragment.bbox or element.bbox
    return (
        max(0.0, left / 1000 * width),
        max(0.0, top / 1000 * height),
        min(width, right / 1000 * width),
        min(height, bottom / 1000 * height),
    )


def _words_in_fragment(
    words: list[tuple],
    bbox: tuple[float, float, float, float],
    margin: float = 3.0,
) -> list[tuple[float, float, float, float, str]]:
    left, top, right, bottom = bbox
    return [
        word
        for word in words
        if left - margin <= (_word_bbox(word)[0] + _word_bbox(word)[2]) / 2 <= right + margin
        and top - margin <= (_word_bbox(word)[1] + _word_bbox(word)[3]) / 2 <= bottom + margin
    ]


def _repair_invalid_fragment_bbox(
    fragment: TextFragment,
    element: PageElement,
    words: list[tuple],
    width: float,
    height: float,
    force: bool = False,
) -> bool:
    bbox = _fragment_bbox_points(fragment, element, width, height)
    if not force and bbox[0] < bbox[2] and bbox[1] < bbox[3]:
        return False
    target_keys = [
        _token_key(token.text)
        for token in exact_text_tokens(fragment.text)
        if _token_key(token.text)
    ]
    if not target_keys:
        return False
    source_keys = [_token_key(_word_text(word)) for word in words]
    matched_boxes: list[tuple[float, float, float, float]] = []
    if force and len(target_keys) < 3:
        occurrences = [
            start
            for start in range(len(source_keys) - len(target_keys) + 1)
            if source_keys[start : start + len(target_keys)] == target_keys
        ]
        if len(occurrences) != 1:
            return False
        start = occurrences[0]
        matched_boxes.extend(
            _word_bbox(word) for word in words[start : start + len(target_keys)]
        )
        matched_tokens = len(target_keys)
    else:
        matcher = SequenceMatcher(None, source_keys, target_keys, autojunk=False)
        matched_tokens = 0
        for block in matcher.get_matching_blocks():
            if not block.size:
                continue
            matched_boxes.extend(
                _word_bbox(word) for word in words[block.a : block.a + block.size]
            )
            matched_tokens += block.size
    if matched_tokens / len(target_keys) < 0.5 or not matched_boxes:
        return False
    repaired = (
        min(box[0] for box in matched_boxes),
        min(box[1] for box in matched_boxes),
        max(box[2] for box in matched_boxes),
        max(box[3] for box in matched_boxes),
    )
    if repaired[0] >= repaired[2] or repaired[1] >= repaired[3]:
        return False
    if force:
        # A page-wide sequence match can collect common words from unrelated
        # regions (for example a repeated event name and footer). A genuine
        # shifted box should move while retaining roughly the same footprint;
        # reject repairs whose scattered matches inflate either dimension.
        original_width = max(bbox[2] - bbox[0], 0.01)
        original_height = max(bbox[3] - bbox[1], 0.01)
        repaired_width = repaired[2] - repaired[0]
        repaired_height = repaired[3] - repaired[1]
        if repaired_width > max(original_width * 3, 144.0) or repaired_height > max(
            original_height * 3, 36.0
        ):
            return False
    fragment.bbox = [
        round(repaired[0] / width * 1000, 3),
        round(repaired[1] / height * 1000, 3),
        round(repaired[2] / width * 1000, 3),
        round(repaired[3] / height * 1000, 3),
    ]
    return True


def _chunks_by_fragment(
    element: PageElement,
    chunks: list[AnchorChunk],
) -> list[tuple[TextFragment, list[AnchorChunk]]]:
    fragments = element.visible_fragments
    if not fragments:
        return []
    if len(fragments) == 1:
        return [(fragments[0], chunks)]

    source_tokens: list[tuple[str, int]] = []
    for fragment_index, fragment in enumerate(fragments):
        source_tokens.extend(
            (token.text, fragment_index) for token in exact_text_tokens(fragment.text)
        )
    source_keys = [_token_key(token) for token, _ in source_tokens]
    corrected_keys = [_token_key(chunk.token_text) for chunk in chunks]
    assignments: list[int | None] = [None] * len(chunks)
    matcher = SequenceMatcher(None, source_keys, corrected_keys, autojunk=False)

    for tag, source_start, source_end, corrected_start, corrected_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(corrected_end - corrected_start):
                assignments[corrected_start + offset] = source_tokens[source_start + offset][1]
            continue
        if tag == "delete" or corrected_start == corrected_end:
            continue
        source_fragments = [
            fragment_index
            for _, fragment_index in source_tokens[source_start:source_end]
        ]
        if not source_fragments:
            if source_start:
                source_fragments = [source_tokens[source_start - 1][1]]
            elif source_start < len(source_tokens):
                source_fragments = [source_tokens[source_start][1]]
        if not source_fragments:
            source_fragments = [0]
        corrected_count = corrected_end - corrected_start
        for offset in range(corrected_count):
            source_offset = min(
                offset * len(source_fragments) // corrected_count,
                len(source_fragments) - 1,
            )
            assignments[corrected_start + offset] = source_fragments[source_offset]

    for index, assignment in enumerate(assignments):
        if assignment is not None:
            continue
        previous = next(
            (assignments[item] for item in range(index - 1, -1, -1) if assignments[item] is not None),
            None,
        )
        following = next(
            (
                assignments[item]
                for item in range(index + 1, len(assignments))
                if assignments[item] is not None
            ),
            None,
        )
        assignments[index] = previous if previous is not None else following or 0

    grouped: list[list[AnchorChunk]] = [[] for _ in fragments]
    for chunk, fragment_index in zip(chunks, assignments, strict=True):
        grouped[int(fragment_index)].append(chunk)
    return list(zip(fragments, grouped, strict=True))


def _align_element_fragments(
    element: PageElement,
    chunks: list[AnchorChunk],
    words: list[tuple],
    width: float,
    height: float,
    geometry_source: str,
) -> dict[int, list[WordPlacement]]:
    placements: dict[int, list[WordPlacement]] = {}
    weighted_quality = 0.0
    weighted_chunks = 0
    for fragment, fragment_chunks in _chunks_by_fragment(element, chunks):
        _repair_invalid_fragment_bbox(
            fragment,
            element,
            words,
            width,
            height,
        )
        bbox = _fragment_bbox_points(fragment, element, width, height)
        local_words = _words_in_fragment(words, bbox)
        local_quality = _alignment_quality(fragment_chunks, local_words)
        if local_quality < 0.5 and _repair_invalid_fragment_bbox(
            fragment,
            element,
            words,
            width,
            height,
            force=True,
        ):
            bbox = _fragment_bbox_points(fragment, element, width, height)
            local_words = _words_in_fragment(words, bbox)
            local_quality = _alignment_quality(fragment_chunks, local_words)
        fragment.geometry_word_count = len(local_words)
        fragment.geometry_source = geometry_source
        fragment.alignment_coverage = local_quality
        chunk_count = max(len(fragment_chunks), 1)
        weighted_quality += fragment.alignment_coverage * chunk_count
        weighted_chunks += chunk_count
        placements.update(
            _align_corrected_words(
                fragment_chunks,
                local_words,
                fallback_bbox=bbox,
            )
        )
    element.evidence.alignment_coverage = (
        weighted_quality / weighted_chunks if weighted_chunks else 0.0
    )
    return placements


def _extract_ocr_words(source: Path) -> list[list[tuple]]:
    pages: list[list[tuple]] = []
    with pymupdf.open(source) as document:
        for page in document:
            pages.append(
                [
                    (
                        float(x0),
                        float(y0),
                        float(x1),
                        float(y1),
                        str(word),
                        int(block_number),
                        int(line_number),
                        int(word_number),
                    )
                    for (
                        x0,
                        y0,
                        x1,
                        y1,
                        word,
                        block_number,
                        line_number,
                        word_number,
                    ) in page.get_text("words", sort=False)
                ]
            )
    return pages


def _alignment_quality(
    chunks: list[AnchorChunk],
    words: list[tuple],
) -> float:
    corrected_keys = [
        _token_key(chunk.token_text) for chunk in chunks if chunk.token_text
    ]
    word_keys = [_token_key(_word_text(word)) for word in words]
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
    base_words: list[list[tuple]],
    candidate_words: list[list[tuple]] | None,
    primary_label: str = "ocr",
    candidate_label: str = "native",
) -> tuple[
    list[list[tuple]],
    list[str],
]:
    selected: list[list[tuple]] = []
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
        actual_text = (
            element.alt_text or ""
            if element.role == ElementRole.FIGURE
            else element.extraction_text
        )
        formula_spans = element.formula_spans
        element_chunks: list[AnchorChunk] = []
        tokens = exact_text_tokens(actual_text)
        for token in tokens:
            boundaries = {token.start, token.actual_end}
            boundaries.update(
                token.start + match.end()
                for match in re.finditer(
                    r"\r\n|\r|\n",
                    actual_text[token.start : token.actual_end],
                )
            )
            for span in formula_spans:
                if token.start < span.start < token.actual_end:
                    boundaries.add(span.start)
                if token.start < span.end < token.actual_end:
                    boundaries.add(span.end)
            ordered_boundaries = sorted(boundaries)
            for text_start, text_end in zip(
                ordered_boundaries, ordered_boundaries[1:]
            ):
                formula_index = next(
                    (
                        index
                        for index, span in enumerate(formula_spans)
                        if span.start <= text_start and text_end <= span.end
                    ),
                    None,
                )
                formula_alt = (
                    formula_spans[formula_index].alt_text
                    if formula_index is not None
                    else None
                )
                token_start = max(text_start, token.start)
                token_end = min(text_end, token.end)
                element_chunks.append(
                    AnchorChunk(
                        element=element,
                        text=actual_text[text_start:text_end],
                        token_text=actual_text[token_start:token_end],
                        mcid=next_mcid,
                        offset=len(element_chunks),
                        text_start=text_start,
                        text_end=text_end,
                        formula_index=formula_index,
                        formula_alt=formula_alt,
                    )
                )
                next_mcid += 1
        by_element.append(element_chunks)
    return by_element


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _same_visual_line(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    overlap = min(left[3], right[3]) - max(left[1], right[1])
    minimum_height = max(min(left[3] - left[1], right[3] - right[1]), 0.01)
    if overlap / minimum_height >= 0.35:
        return True
    left_center = (left[1] + left[3]) / 2
    right_center = (right[1] + right[3]) / 2
    maximum_height = max(left[3] - left[1], right[3] - right[1], 0.01)
    return abs(left_center - right_center) <= maximum_height * 0.3


def _reviewed_region_bbox(
    region: StructureRegion | StructureSegment,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    return _union_bbox(
        [
            _fragment_bbox_points(fragment, region.element, width, height)
            for fragment in region.fragments
        ]
    )


def _has_usable_line_evidence(region: StructureRegion | StructureSegment) -> bool:
    return any(
        fragment.geometry_word_count > 0
        and (fragment.alignment_coverage or 0.0) >= 0.5
        for fragment in region.fragments
    )


def _synthetic_region_lines(
    chunks: list[AnchorChunk],
    bbox: tuple[float, float, float, float],
) -> list[LinePlacement]:
    x0, y0, x1, y1 = bbox
    maximum_characters = max(12, int((x1 - x0) / 5.2))
    wrapped_chunks: list[list[AnchorChunk]] = []
    current: list[AnchorChunk] = []
    current_length = 0
    for chunk in chunks:
        if current and current_length + len(chunk.text) > maximum_characters:
            wrapped_chunks.append(current)
            current = []
            current_length = 0
        current.append(chunk)
        current_length += len(chunk.text)
        if "\n" in chunk.text:
            wrapped_chunks.append(current)
            current = []
            current_length = 0
    if current:
        wrapped_chunks.append(current)
    if not wrapped_chunks:
        return []
    line_height = max(5.0, min(12.0, (y1 - y0) / len(wrapped_chunks)))
    return [
        LinePlacement(
            text="".join(chunk.text for chunk in line_chunks),
            bbox=(x0, y0 + index * line_height, x1, y0 + (index + 1) * line_height),
            chunks=line_chunks,
        )
        for index, line_chunks in enumerate(wrapped_chunks)
    ]


def _region_lines(
    region: StructureRegion | StructureSegment,
    placements: dict[int, list[WordPlacement]],
    width: float,
    height: float,
) -> list[LinePlacement]:
    fallback = _reviewed_region_bbox(region, width, height)
    if not _has_usable_line_evidence(region):
        return _synthetic_region_lines(region.chunks, fallback)
    grouped: list[
        tuple[list[AnchorChunk], list[tuple[float, float, float, float]], tuple[int, int] | None]
    ] = []
    for chunk in region.chunks:
        word_placements = placements.get(chunk.mcid, [])
        if not word_placements and not chunk.text.strip() and grouped:
            # Transformation boundaries can isolate a space into its own chunk.
            # It has no source word box and belongs to the preceding physical run;
            # assigning the whole region fallback would collapse every visual line
            # into one giant rectangle and create false anchor collisions.
            grouped[-1][0].append(chunk)
            continue
        boxes = [placement.bbox for placement in word_placements] or [fallback]
        bbox = _union_bbox(boxes)
        line_key = next(
            (
                placement.line_key
                for placement in word_placements
                if placement.line_key is not None
            ),
            None,
        )
        if grouped:
            prior_chunks, prior_boxes, prior_key = grouped[-1]
            prior_bbox = _union_bbox(prior_boxes)
            if _same_visual_line(prior_bbox, bbox) or (
                prior_key is not None and prior_key == line_key
            ):
                prior_chunks.append(chunk)
                prior_boxes.extend(boxes)
                if prior_key is None and line_key is not None:
                    grouped[-1] = (prior_chunks, prior_boxes, line_key)
                continue
        grouped.append(([chunk], list(boxes), line_key))

    lines = [
        LinePlacement(
            text="".join(chunk.text for chunk in chunks),
            bbox=_union_bbox(boxes),
            chunks=chunks,
        )
        for chunks, boxes, _ in grouped
    ]
    if len(lines) != 1 or any(
        placement.line_key is not None
        for chunk in region.chunks
        for placement in placements.get(chunk.mcid, [])
    ):
        return lines

    maximum_characters = max(12, int((lines[0].bbox[2] - lines[0].bbox[0]) / 5.2))
    if len(lines[0].text) <= maximum_characters:
        return lines
    return _synthetic_region_lines(region.chunks, lines[0].bbox)


def _text_advance(text: str, font: AnchorFont) -> int:
    fallback = font.advances.get(ord("?"), 600)
    return sum(font.advances.get(ord(character), fallback) for character in text)


def _anchor_text_metrics(
    text: str,
    bbox: tuple[float, float, float, float],
    font: AnchorFont,
) -> tuple[float, float]:
    """Return font size and horizontal scale used by a direct-text anchor."""
    x0, y0, x1, y1 = bbox
    font_size = max(1.0, min(72.0, (y1 - y0) * 0.82))
    advance_em = max(_text_advance(text, font) / 1000, 0.01)
    available_width = max(x1 - x0, 0.01)
    font_size = max(1.0, min(font_size, available_width / advance_em))
    natural_width = max(advance_em * font_size, 0.01)
    horizontal_scale = max(
        10.0,
        min(1000.0, available_width / natural_width * 100),
    )
    return font_size, horizontal_scale


def _effective_space_advance(
    line: LinePlacement,
    font: AnchorFont,
) -> float:
    if " " not in line.text:
        return float("inf")
    direct_text = _direct_unicode_text(line.text, font.supported_codepoints)
    font_size, horizontal_scale = _anchor_text_metrics(
        direct_text, line.bbox, font
    )
    fallback = font.advances.get(ord("?"), 600)
    space_advance = font.advances.get(ord(" "), fallback) / 1000
    return space_advance * font_size * horizontal_scale / 100


def _widen_dense_logical_line(
    line: LinePlacement,
    obstacles: list[LinePlacement],
    page_width: float,
    anchor_font: AnchorFont,
) -> LinePlacement:
    """Give a dense semantic line more invisible horizontal room when it is safe.

    The structure and Layout BBox continue to describe the printed paragraph. This box
    is only the placement corridor for its invisible direct-Unicode run, so extending it
    through otherwise unused page space can preserve explicit word spaces without
    turning visual wrapping into copy/paste line breaks.
    """
    minimum_space_advance = 0.25
    if _effective_space_advance(line, anchor_font) >= minimum_space_advance:
        return line

    x0, y0, x1, y1 = line.bbox
    margin = min(18.0, page_width * 0.03)
    gap = max(2.0, page_width * 0.005)
    left_limit = min(x0, margin)
    right_limit = max(x1, page_width - margin)
    for other in obstacles:
        if not _same_visual_line(line.bbox, other.bbox):
            continue
        other_x0, _, other_x1, _ = other.bbox
        if other_x1 <= x0:
            left_limit = max(left_limit, other_x1 + gap)
        elif other_x0 >= x1:
            right_limit = min(right_limit, other_x0 - gap)
        else:
            # The original logical boxes already compete. The ordinary collision
            # fallback must retain their non-overlapping printed fragments.
            return line

    candidates = [
        (x0, y0, right_limit, y1),
        (left_limit, y0, right_limit, y1),
    ]
    for bbox in candidates:
        if bbox[0] >= bbox[2] or bbox == line.bbox:
            continue
        widened = LinePlacement(text=line.text, bbox=bbox, chunks=line.chunks)
        if _effective_space_advance(widened, anchor_font) >= minimum_space_advance:
            return widened
    return line


def _logical_region_lines(
    chunks: list[AnchorChunk],
    lines: list[LinePlacement],
    placements: dict[int, list[WordPlacement]],
) -> list[LinePlacement]:
    """Place one text run per explicit semantic line, ignoring visual wrapping."""
    if not lines:
        return []
    groups: list[list[AnchorChunk]] = []
    current: list[AnchorChunk] = []
    for chunk in chunks:
        current.append(chunk)
        if "\n" in chunk.text or "\r" in chunk.text:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if not groups:
        return []

    fallback = _union_bbox([line.bbox for line in lines])
    x0, y0, x1, y1 = fallback
    fallback_height = (y1 - y0) / len(groups)
    logical_lines: list[LinePlacement] = []
    for index, group in enumerate(groups):
        boxes = [
            placement.bbox
            for chunk in group
            for placement in placements.get(chunk.mcid, [])
        ]
        bbox = (
            _union_bbox(boxes)
            if boxes
            else (
                x0,
                y0 + index * fallback_height,
                x1,
                y0 + (index + 1) * fallback_height,
            )
        )
        logical_lines.append(
            LinePlacement(
                text="".join(chunk.text for chunk in group),
                bbox=bbox,
                chunks=group,
            )
        )
    return logical_lines


def _anchor_lines_overlap(left: LinePlacement, right: LinePlacement) -> bool:
    """Whether two invisible direct-text runs would compete in spatial extraction."""
    if not _same_visual_line(left.bbox, right.bbox):
        return False
    horizontal_overlap = min(left.bbox[2], right.bbox[2]) - max(
        left.bbox[0], right.bbox[0]
    )
    narrower_width = max(
        min(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0]),
        0.01,
    )
    return horizontal_overlap > max(1.0, narrower_width * 0.05)


def _line_layouts_differ(
    logical: list[LinePlacement], fragment: list[LinePlacement]
) -> bool:
    if len(logical) != len(fragment):
        return True
    return any(
        left.text != right.text
        or any(abs(a - b) > 0.5 for a, b in zip(left.bbox, right.bbox, strict=True))
        for left, right in zip(logical, fragment, strict=True)
    )


def _colliding_region_pairs(
    selected_lines: list[list[LinePlacement]],
) -> list[tuple[int, int]]:
    collisions: list[tuple[int, int]] = []
    for left_index, left_lines in enumerate(selected_lines):
        for right_index in range(left_index + 1, len(selected_lines)):
            if any(
                _anchor_lines_overlap(left, right)
                for left in left_lines
                for right in selected_lines[right_index]
            ):
                collisions.append((left_index, right_index))
    return collisions


def _collision_safe_region_lines(
    candidates: list[RegionLineCandidate],
    anchor_font: AnchorFont,
    page_width: float,
) -> tuple[list[list[LinePlacement]], set[int]]:
    """Keep logical de-wrapping unless it creates overlapping invisible runs.

    A logical element can have a non-rectangular footprint when it wraps around another
    element or shares a printed line with the next element. Collapsing all of its words
    into their union rectangle makes Acrobat spatially interleave the direct Unicode
    anchors. In that case, retain the element's structure but place its text at the
    reviewed physical fragments.
    """
    original_lines = [
        line for candidate in candidates for line in candidate.logical_lines
    ]
    selected = [
        [
            _widen_dense_logical_line(
                line,
                [other for other in original_lines if other is not line],
                page_width,
                anchor_font,
            )
            for line in candidate.logical_lines
        ]
        for candidate in candidates
    ]
    fragment_anchored: set[int] = set()
    for index, candidate in enumerate(candidates):
        if not _line_layouts_differ(
            selected[index], candidate.fragment_lines
        ):
            continue
        if any(
            _effective_space_advance(line, anchor_font) < 0.25
            for line in selected[index]
        ):
            selected[index] = candidate.fragment_lines
            fragment_anchored.add(index)

    while True:
        changed = False
        for left_index, right_index in _colliding_region_pairs(selected):
            for index in (left_index, right_index):
                candidate = candidates[index]
                if index in fragment_anchored or not _line_layouts_differ(
                    candidate.logical_lines, candidate.fragment_lines
                ):
                    continue
                selected[index] = candidate.fragment_lines
                fragment_anchored.add(index)
                changed = True
        if not changed:
            break

    unresolved = _colliding_region_pairs(selected)
    if unresolved:
        examples = []
        for left_index, right_index in unresolved[:3]:
            left = candidates[left_index].element
            right = candidates[right_index].element
            examples.append(
                f"{left.id or left.role.value} overlaps {right.id or right.role.value}"
            )
        raise RuntimeError(
            "Invisible accessibility text runs still overlap after fragment-aware "
            f"placement: {'; '.join(examples)}"
        )
    return selected, fragment_anchored


def _anchor_stream(
    region: StructureRegion,
    width: float,
    height: float,
    anchor_font: AnchorFont,
    placements: dict[int, list[WordPlacement]] | None = None,
) -> bytes:
    placements = placements or {}
    if region.element.role == ElementRole.FIGURE:
        bbox = _reviewed_region_bbox(region, width, height)
        x0, y0, x1, y1 = bbox
        pdf_y = height - y1
        mcid = region.segments[0].mcid
        return (
            f"/Figure <</MCID {mcid}>> BDC\n"
            "q\n"
            f"{x0:.3f} {pdf_y:.3f} {x1 - x0:.3f} {y1 - y0:.3f} re W n\n"
            "Q\n"
            "EMC\n"
        ).encode("ascii")

    if (
        len(region.segments) == 1
        and region.segments[0].formula_alt is None
        and region.segments[0].line_index == -1
    ):
        segment = region.segments[0]
        exact_text = "".join(chunk.text for chunk in region.chunks)
        direct_lines = [
            _direct_unicode_text(line.text, anchor_font.supported_codepoints)
            for line in region.lines
        ]
        properties = f"/MCID {segment.mcid}"
        use_actual_text = "".join(direct_lines) != exact_text
        actual_text_properties = ""
        if use_actual_text:
            actual_text = b"\xfe\xff" + exact_text.encode("utf-16-be")
            actual_text_properties = (
                f"/Span <</ActualText <{actual_text.hex().upper()}>>> BDC"
            )
        commands = [
            f"{ROLE_NAMES[region.element.role]} <<{properties}>> BDC",
        ]
        if actual_text_properties:
            commands.append(actual_text_properties)
        commands.append("BT")
        for line, direct_text in zip(region.lines, direct_lines, strict=True):
            x0, y0, x1, y1 = line.bbox
            font_size, horizontal_scale = _anchor_text_metrics(
                direct_text, line.bbox, anchor_font
            )
            baseline = height - y1 + font_size * 0.18
            encoded = direct_text.encode("utf-16-be")
            commands.append(
                f"/A11yAnchor {font_size:.3f} Tf 3 Tr {horizontal_scale:.3f} Tz "
                f"1 0 0 1 {x0:.3f} {baseline:.3f} Tm "
                f"<{encoded.hex().upper()}> Tj"
            )
        commands.append("ET")
        if actual_text_properties:
            commands.append("EMC")
        commands.append("EMC")
        return ("\n".join(commands) + "\n").encode("ascii")

    commands: list[str] = ["BT"]
    for line_index, line in enumerate(region.lines):
        direct_line = _direct_unicode_text(
            line.text, anchor_font.supported_codepoints
        )
        x0, y0, x1, y1 = line.bbox
        font_size, horizontal_scale = _anchor_text_metrics(
            direct_line, line.bbox, anchor_font
        )
        baseline = height - y1 + font_size * 0.18
        commands.append(
            f"/A11yAnchor {font_size:.3f} Tf 3 Tr {horizontal_scale:.3f} Tz "
            f"1 0 0 1 {x0:.3f} {baseline:.3f} Tm"
        )
        for segment in (
            item for item in region.segments if item.line_index == line_index
        ):
            exact_text = "".join(chunk.text for chunk in segment.chunks)
            direct_text = _direct_unicode_text(
                exact_text, anchor_font.supported_codepoints
            )
            properties = f"/MCID {segment.mcid}"
            if direct_text != exact_text:
                actual_text = b"\xfe\xff" + exact_text.encode("utf-16-be")
                properties += f" /ActualText <{actual_text.hex().upper()}>"
            content_role = (
                Name("/Formula")
                if segment.formula_alt
                else ROLE_NAMES[region.element.role]
            )
            encoded = direct_text.encode("utf-16-be")
            commands.extend(
                [
                    f"{content_role} <<{properties}>> BDC",
                    f"<{encoded.hex().upper()}> Tj",
                    "EMC",
                ]
            )
    commands.append("ET")
    return ("\n".join(commands) + "\n").encode("ascii")


def _element_structure_regions(
    element: PageElement,
    chunks: list[AnchorChunk],
) -> list[tuple[str, list[AnchorChunk], list[TextFragment]]]:
    regions: list[tuple[str, list[AnchorChunk], list[TextFragment]]] = [
        (element.id, chunks, element.visible_fragments)
    ]
    if element.role == ElementRole.P and len(element.visible_fragments) > 1:
        fragment_chunks = _chunks_by_fragment(element, chunks)
        groups = fragment_region_groups(element.visible_fragments)
        if len(groups) > 1:
            candidate_regions = []
            for group in groups:
                grouped_chunks = [
                    chunk
                    for fragment_index in group
                    for chunk in fragment_chunks[fragment_index][1]
                ]
                if grouped_chunks:
                    candidate_regions.append(
                        (
                            element.visible_fragments[group[0]].id,
                            grouped_chunks,
                            [element.visible_fragments[index] for index in group],
                        )
                    )
            if "".join(
                chunk.text
                for _, region_chunks, _ in candidate_regions
                for chunk in region_chunks
            ) == "".join(chunk.text for chunk in chunks):
                regions = candidate_regions
    return regions


def _make_role_element(
    pdf: pikepdf.Pdf,
    region: StructureRegion,
    page_obj: pikepdf.Object,
    parent: pikepdf.Object,
    placements: dict[int, list[WordPlacement]],
    width: float,
    height: float,
    role_name: pikepdf.Name | None = None,
) -> tuple[pikepdf.Object, list[pikepdf.Object]]:
    element = region.element
    simple_content = (
        len(region.segments) == 1 and region.segments[0].formula_alt is None
    )
    role_element = pdf.make_indirect(
        Dictionary(
            Type=Name.StructElem,
            S=role_name or ROLE_NAMES[element.role],
            P=parent,
            Pg=page_obj,
            K=(region.segments[0].mcid if simple_content else Array()),
        )
    )
    mcid_parents: list[pikepdf.Object] = []
    if element.role == ElementRole.FIGURE:
        role_element[Name.Alt] = String(element.alt_text or "")
    elif region.fragment_anchored and simple_content:
        # Preserve the de-wrapped semantic string for viewers that honor
        # structure-level replacement text while direct Unicode remains aligned
        # to the non-overlapping visual fragments for spatial extractors.
        role_element[Name.ActualText] = String(
            "".join(chunk.text for chunk in region.chunks)
        )

    if simple_content:
        mcid_parents.append(role_element)
    else:
        for formula_index, grouped_segments in groupby(
            region.segments, key=lambda segment: segment.formula_index
        ):
            segments = list(grouped_segments)
            if formula_index is None:
                for segment in segments:
                    role_element[Name.K].append(segment.mcid)
                    mcid_parents.append(role_element)
                continue
            formula_content = (
                segments[0].mcid
                if len(segments) == 1
                else Array([segment.mcid for segment in segments])
            )
            formula = pdf.make_indirect(
                Dictionary(
                    Type=Name.StructElem,
                    S=Name("/Formula"),
                    P=role_element,
                    Pg=page_obj,
                    K=formula_content,
                    Alt=String(segments[0].formula_alt or ""),
                )
            )
            formula_boxes = [
                placement.bbox
                for segment in segments
                for chunk in segment.chunks
                for placement in placements.get(chunk.mcid, [])
            ] or [_reviewed_region_bbox(segments[0], width, height)]
            formula[Name.A] = Dictionary(
                O=Name.Layout,
                BBox=Array(
                    [
                        round(min(box[0] for box in formula_boxes), 3),
                        round(height - max(box[3] for box in formula_boxes), 3),
                        round(max(box[2] for box in formula_boxes), 3),
                        round(height - min(box[1] for box in formula_boxes), 3),
                    ]
                ),
            )
            role_element[Name.K].append(formula)
            mcid_parents.extend([formula] * len(segments))

    boxes = (
        [_reviewed_region_bbox(region, width, height)]
        if element.role == ElementRole.FIGURE or not _has_usable_line_evidence(region)
        else [
            placement.bbox
            for chunk in region.chunks
            for placement in placements.get(chunk.mcid, [])
        ]
    )
    if boxes:
        role_element[Name.A] = Dictionary(
            O=Name.Layout,
            BBox=Array(
                [
                    round(min(box[0] for box in boxes), 3),
                    round(height - max(box[3] for box in boxes), 3),
                    round(max(box[2] for box in boxes), 3),
                    round(height - min(box[1] for box in boxes), 3),
                ]
            ),
        )
    return role_element, mcid_parents


def _placement_bbox(
    chunks: list[AnchorChunk],
    placements: dict[int, list[WordPlacement]],
    width: float,
    height: float,
) -> list[float] | None:
    boxes = [
        placement.bbox
        for chunk in chunks
        for placement in placements.get(chunk.mcid, [])
    ]
    if not boxes:
        return None
    return [
        round(min(box[0] for box in boxes) / width * 1000, 3),
        round(min(box[1] for box in boxes) / height * 1000, 3),
        round(max(box[2] for box in boxes) / width * 1000, 3),
        round(max(box[3] for box in boxes) / height * 1000, 3),
    ]


def compile_tagged_pdf(
    source: Path,
    output: Path,
    plan: DocumentPlan,
    geometry_source: Path | None = None,
    base_geometry_label: str = "ocr",
    candidate_geometry_label: str = "native",
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
        primary_label=base_geometry_label,
        candidate_label=candidate_geometry_label,
    )
    with pikepdf.Pdf.open(source) as pdf:
        anchor_font = _make_anchor_font(pdf, plan)
        structure_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        document = pdf.make_indirect(
            Dictionary(Type=Name.StructElem, S=Name.Document, P=structure_root, K=Array())
        )
        structure_root[Name.K] = document

        parent_tree_entries = Array()
        plan_by_page = {page.page_number: page for page in plan.pages}
        sanitized_xobjects: set[tuple[int, int]] = set()
        next_annotation_parent = len(pdf.pages)

        for page_index, page in enumerate(pdf.pages):
            page_plan = plan_by_page.get(page_index + 1)
            if page_plan is None:
                continue

            _strip_form_xobject_marking(page.Resources, sanitized_xobjects)
            original_contents = _source_content_without_marking(pdf, page)
            # Keep the source's visible page operators intact while preventing their
            # untagged text from duplicating the reviewed semantic anchor layer in
            # ordinary text extraction. A single-space ActualText replacement is
            # ignored by token extractors while still being honored by Poppler.
            artifact_start = pdf.make_indirect(
                Stream(
                    pdf,
                    b"q\n/Artifact BMC\n/Span <</ActualText <FEFF0020>>> BDC\n",
                )
            )
            artifact_end = pdf.make_indirect(Stream(pdf, b"EMC\nEMC\nQ\n"))
            width = float(page.mediabox[2]) - float(page.mediabox[0])
            height = float(page.mediabox[3]) - float(page.mediabox[1])
            _ensure_anchor_font(page, anchor_font)
            chunks_by_element = _page_anchor_chunks(page_plan.elements)
            _suppress_ocr_text(page)
            placements: dict[int, list[WordPlacement]] = {}
            for element, chunks in zip(
                page_plan.elements, chunks_by_element, strict=True
            ):
                placements.update(
                    _align_element_fragments(
                        element,
                        chunks,
                        geometry_words_by_page[page_index],
                        width,
                        height,
                        geometry_sources[page_index],
                    )
                )
            if placements:
                for element, chunks in zip(
                    page_plan.elements, chunks_by_element, strict=True
                ):
                    if (
                        element.role == ElementRole.FIGURE
                        or element.evidence.alignment_coverage < 0.5
                    ):
                        continue
                    derived_bbox = _placement_bbox(chunks, placements, width, height)
                    if derived_bbox:
                        element.bbox = derived_bbox
            candidates_by_element: list[list[RegionLineCandidate]] = []
            for element, chunks in zip(
                page_plan.elements, chunks_by_element, strict=True
            ):
                element_candidates = []
                for _, region_chunks, region_fragments in _element_structure_regions(
                    element, chunks
                ):
                    region_shell = StructureRegion(
                        element=element,
                        chunks=region_chunks,
                        fragments=region_fragments,
                        lines=[],
                        segments=[],
                    )
                    if element.role == ElementRole.FIGURE:
                        fragment_lines: list[LinePlacement] = []
                        logical_lines: list[LinePlacement] = []
                    else:
                        fragment_lines = _region_lines(
                            region_shell, placements, width, height
                        )
                        logical_lines = _logical_region_lines(
                            region_chunks, fragment_lines, placements
                        )
                    element_candidates.append(
                        RegionLineCandidate(
                            element=element,
                            chunks=region_chunks,
                            fragments=region_fragments,
                            logical_lines=logical_lines,
                            fragment_lines=fragment_lines,
                        )
                    )
                candidates_by_element.append(element_candidates)

            candidates = [
                candidate
                for element_candidates in candidates_by_element
                for candidate in element_candidates
            ]
            selected_lines, fragment_anchored = _collision_safe_region_lines(
                candidates, anchor_font, width
            )

            regions_by_element: list[list[StructureRegion]] = []
            next_region_mcid = 0
            candidate_index = 0
            for element_candidates in candidates_by_element:
                element_regions = []
                for candidate in element_candidates:
                    lines = selected_lines[candidate_index]
                    segments: list[StructureSegment] = []
                    if candidate.element.role == ElementRole.FIGURE:
                        segments.append(
                            StructureSegment(
                                element=candidate.element,
                                chunks=candidate.chunks,
                                fragments=candidate.fragments,
                                mcid=next_region_mcid,
                                line_index=0,
                            )
                        )
                        next_region_mcid += 1
                    else:
                        has_formula = any(
                            chunk.formula_index is not None
                            for chunk in candidate.chunks
                        )
                        if not has_formula:
                            segments.append(
                                StructureSegment(
                                    element=candidate.element,
                                    chunks=candidate.chunks,
                                    fragments=candidate.fragments,
                                    mcid=next_region_mcid,
                                    line_index=-1,
                                )
                            )
                            next_region_mcid += 1
                        else:
                            for line_index, line in enumerate(lines):
                                for formula_index, grouped_chunks in groupby(
                                    line.chunks, key=lambda chunk: chunk.formula_index
                                ):
                                    segment_chunks = list(grouped_chunks)
                                    segments.append(
                                        StructureSegment(
                                            element=candidate.element,
                                            chunks=segment_chunks,
                                            fragments=candidate.fragments,
                                            mcid=next_region_mcid,
                                            line_index=line_index,
                                            formula_index=formula_index,
                                            formula_alt=(
                                                segment_chunks[0].formula_alt
                                                if formula_index is not None
                                                else None
                                            ),
                                        )
                                    )
                                    next_region_mcid += 1
                    element_regions.append(
                        StructureRegion(
                            element=candidate.element,
                            chunks=candidate.chunks,
                            fragments=candidate.fragments,
                            lines=lines,
                            segments=segments,
                            fragment_anchored=candidate_index in fragment_anchored,
                        )
                    )
                    candidate_index += 1
                regions_by_element.append(element_regions)
            regions = [
                region
                for element_regions in regions_by_element
                for region in element_regions
            ]
            anchor_streams = [
                pdf.make_indirect(
                    Stream(
                        pdf,
                        _anchor_stream(
                            region,
                            width,
                            height,
                            anchor_font,
                            placements,
                        ),
                    )
                )
                for region in regions
            ]
            page.obj[Name.Contents] = Array(
                [artifact_start, *original_contents, artifact_end, *anchor_streams]
            )
            page.obj[Name.StructParents] = page_index
            page.obj[Name.Tabs] = Name.S

            mcid_parents: list[pikepdf.Object] = []
            active_list: pikepdf.Object | None = None
            for element, element_regions in zip(
                page_plan.elements, regions_by_element, strict=True
            ):
                if element.role == ElementRole.LI:
                    if active_list is None:
                        active_list = pdf.make_indirect(
                            Dictionary(
                                Type=Name.StructElem,
                                S=Name.L,
                                P=document,
                                K=Array(),
                                A=Dictionary(
                                    O=Name.List,
                                    ListNumbering=Name("/None"),
                                ),
                            )
                        )
                        document[Name.K].append(active_list)
                    list_item = pdf.make_indirect(
                        Dictionary(
                            Type=Name.StructElem,
                            S=Name.LI,
                            P=active_list,
                            Pg=page.obj,
                            K=Array(),
                        )
                    )
                    active_list[Name.K].append(list_item)
                    for region in element_regions:
                        list_body, owners = _make_role_element(
                            pdf,
                            region,
                            page.obj,
                            list_item,
                            placements,
                            width,
                            height,
                            role_name=Name.LBody,
                        )
                        list_item[Name.K].append(list_body)
                        mcid_parents.extend(owners)
                    continue

                active_list = None
                for region in element_regions:
                    role_element, owners = _make_role_element(
                        pdf,
                        region,
                        page.obj,
                        document,
                        placements,
                        width,
                        height,
                    )
                    document[Name.K].append(role_element)
                    mcid_parents.extend(owners)

            parent_tree_entries.extend([page_index, Array(mcid_parents)])
            for annotation in page.obj.get(Name.Annots, []):
                if str(annotation.get(Name.Subtype, "")) != "/Link":
                    continue
                description = _link_description(annotation)
                annotation[Name.Contents] = String(description)
                annotation[Name.StructParent] = next_annotation_parent
                object_reference = pdf.make_indirect(
                    Dictionary(Type=Name.OBJR, Obj=annotation, Pg=page.obj)
                )
                link = pdf.make_indirect(
                    Dictionary(
                        Type=Name.StructElem,
                        S=Name.Link,
                        P=document,
                        Pg=page.obj,
                        K=object_reference,
                        Alt=String(description),
                    )
                )
                document[Name.K].append(link)
                parent_tree_entries.extend([next_annotation_parent, link])
                next_annotation_parent += 1

        parent_tree = pdf.make_indirect(Dictionary(Nums=parent_tree_entries))
        structure_root[Name.ParentTree] = parent_tree
        structure_root[Name.ParentTreeNextKey] = next_annotation_parent

        pdf.Root[Name.StructTreeRoot] = structure_root
        pdf.Root[Name.MarkInfo] = Dictionary(Marked=True)
        pdf.Root[Name.Lang] = String(plan.language)
        pdf.Root[Name.ViewerPreferences] = Dictionary(DisplayDocTitle=True)
        pdf.Root[Name.PageLabels] = Dictionary(
            Nums=Array([0, Dictionary(S=Name("/D"), St=1)])
        )
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
        # The validated release stage performs final linearization. Keeping the
        # intermediate save ordinary avoids qpdf object renumbering edge cases in
        # source PDFs with large, densely shared object graphs.
        pdf.save(output, force_version="1.7")
    return geometry_sources
