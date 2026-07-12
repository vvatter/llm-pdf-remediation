from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pikepdf
import pymupdf
from pydantic import BaseModel, Field

from .models import RemediationMode
from .plans import sha256_file


class FontFinding(BaseModel):
    page: int
    name: str
    font_type: str
    embedded: bool
    unicode_mapping: bool


class PagePreflight(BaseModel):
    page_number: int
    blank: bool
    text_characters: int
    invalid_character_ratio: float
    usable_native_text: bool


class PreflightReport(BaseModel):
    source: str
    source_sha256: str
    requested_mode: RemediationMode
    selected_mode: RemediationMode
    page_count: int
    encrypted: bool
    qpdf_ok: bool
    render_ok: bool
    already_tagged: bool
    pdfua_valid: bool | None
    native_text_page_ratio: float
    fonts_embedded: bool
    fonts_unicode_mapped: bool
    pages: list[PagePreflight] = Field(default_factory=list)
    fonts: list[FontFinding] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def run_verapdf(pdf_path: Path) -> tuple[bool | None, dict[str, object] | None]:
    executable = os.getenv("VERAPDF") or shutil.which("verapdf")
    if not executable:
        return None, None
    completed = subprocess.run(
        [executable, "--format", "json", "--flavour", "ua1", str(pdf_path)],
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(completed.stdout)
        result = report["report"]["jobs"][0]["validationResult"][0]
        return bool(result["compliant"]), report
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False, {"stderr": completed.stderr, "stdout": completed.stdout}


def _qpdf_ok(path: Path) -> bool:
    executable = shutil.which("qpdf")
    if not executable:
        return False
    return subprocess.run(
        [executable, "--check", str(path)], capture_output=True, text=True
    ).returncode in {0, 3}


def _invalid_character_ratio(text: str) -> float:
    if not text:
        return 1.0
    invalid = sum(
        character == "\ufffd"
        or ord(character) in range(0x00, 0x09)
        or ord(character) in range(0x0E, 0x20)
        for character in text
    )
    return invalid / len(text)


def _is_blank(page: pymupdf.Page) -> bool:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), colorspace=pymupdf.csGRAY)
    samples = pixmap.samples
    if not samples:
        return True
    nonwhite = sum(value < 245 for value in samples)
    return nonwhite / len(samples) < 0.001


def inspect_pdf(
    source: Path,
    requested_mode: RemediationMode = RemediationMode.AUTO,
) -> PreflightReport:
    source = source.resolve()
    qpdf_ok = _qpdf_ok(source)
    pages: list[PagePreflight] = []
    fonts: list[FontFinding] = []
    render_ok = True
    encrypted = False
    already_tagged = False

    try:
        with pikepdf.Pdf.open(source) as pdf:
            encrypted = bool(pdf.is_encrypted)
            already_tagged = bool(
                pdf.Root.get("/StructTreeRoot")
                and pdf.Root.get("/MarkInfo", {}).get("/Marked", False)
            )
    except (pikepdf.PdfError, pikepdf.PasswordError):
        encrypted = True

    try:
        with pymupdf.open(source) as document:
            for page_number, page in enumerate(document, start=1):
                try:
                    blank = _is_blank(page)
                except Exception:
                    blank = False
                    render_ok = False
                text = page.get_text("text")
                characters = len("".join(text.split()))
                invalid_ratio = _invalid_character_ratio(text)
                pages.append(
                    PagePreflight(
                        page_number=page_number,
                        blank=blank,
                        text_characters=characters,
                        invalid_character_ratio=invalid_ratio,
                        usable_native_text=blank
                        or (characters >= 50 and invalid_ratio <= 0.005),
                    )
                )
                for xref, extension, font_type, base_font, resource_name, encoding, *_ in page.get_fonts(full=True):
                    embedded = extension not in {"n/a", ""}
                    to_unicode_type, _ = document.xref_get_key(xref, "ToUnicode")
                    unicode_mapping = (
                        encoding in {"WinAnsiEncoding", "MacRomanEncoding", "MacExpertEncoding"}
                        or to_unicode_type not in {"null", "none"}
                    )
                    fonts.append(
                        FontFinding(
                            page=page_number,
                            name=base_font or resource_name or str(xref),
                            font_type=font_type,
                            embedded=embedded,
                            unicode_mapping=unicode_mapping,
                        )
                    )
    except Exception:
        render_ok = False

    nonblank = [page for page in pages if not page.blank]
    native_ratio = (
        sum(page.usable_native_text for page in nonblank) / len(nonblank)
        if nonblank
        else 1.0
    )
    fonts_embedded = bool(fonts) and all(font.embedded for font in fonts)
    fonts_unicode = bool(fonts) and all(font.unicode_mapping for font in fonts)
    pdfua_valid: bool | None = None
    reasons: list[str] = []

    if encrypted:
        automatic = RemediationMode.UNSUPPORTED
        reasons.append("The PDF is encrypted or cannot be opened without a password.")
    elif not qpdf_ok or not render_ok:
        automatic = RemediationMode.UNSUPPORTED
        reasons.append("The PDF failed structural or rendering preflight.")
    elif already_tagged:
        pdfua_valid, _ = run_verapdf(source)
        if pdfua_valid:
            automatic = RemediationMode.PASS_THROUGH
            reasons.append("The existing tagged PDF passes veraPDF PDF/UA-1 validation.")
        else:
            automatic = (
                RemediationMode.NATIVE
                if native_ratio >= 0.95 and fonts_embedded and fonts_unicode
                else RemediationMode.FACSIMILE
            )
            reasons.append("Existing tags are not a validated PDF/UA-1 pass-through candidate.")
    elif native_ratio >= 0.95 and fonts_embedded and fonts_unicode:
        automatic = RemediationMode.NATIVE
        reasons.append("At least 95% of nonblank pages have usable native Unicode text and fonts.")
    else:
        automatic = RemediationMode.FACSIMILE
        if native_ratio < 0.95:
            reasons.append(f"Usable native text covers only {native_ratio:.1%} of nonblank pages.")
        if not fonts_embedded:
            reasons.append("One or more used fonts are not embedded.")
        if not fonts_unicode:
            reasons.append("One or more used fonts lack a reliable Unicode encoding.")

    if requested_mode == RemediationMode.PASS_THROUGH and automatic != RemediationMode.PASS_THROUGH:
        selected = RemediationMode.UNSUPPORTED
        reasons.append("Pass-through cannot be forced unless the existing PDF passes PDF/UA-1 validation.")
    else:
        selected = (
            automatic
            if requested_mode == RemediationMode.AUTO or automatic == RemediationMode.UNSUPPORTED
            else requested_mode
        )
    if requested_mode != RemediationMode.AUTO and selected == requested_mode and selected != automatic:
        reasons.append(f"Automatic mode {automatic.value!r} was overridden with {selected.value!r}.")

    return PreflightReport(
        source=str(source),
        source_sha256=sha256_file(source),
        requested_mode=requested_mode,
        selected_mode=selected,
        page_count=len(pages),
        encrypted=encrypted,
        qpdf_ok=qpdf_ok,
        render_ok=render_ok,
        already_tagged=already_tagged,
        pdfua_valid=pdfua_valid,
        native_text_page_ratio=native_ratio,
        fonts_embedded=fonts_embedded,
        fonts_unicode_mapped=fonts_unicode,
        pages=pages,
        fonts=fonts,
        reasons=reasons,
    )


def write_preflight(report: PreflightReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
