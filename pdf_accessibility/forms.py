from __future__ import annotations

from pathlib import Path
import re
from typing import Iterator

import pikepdf


def inherited_field_value(widget: pikepdf.Object, key: str) -> object | None:
    """Return a widget's inheritable field value from its nearest ancestor."""
    current: pikepdf.Object | None = widget
    visited: set[tuple[int, int]] = set()
    while isinstance(current, pikepdf.Dictionary):
        identity = current.objgen
        if identity != (0, 0):
            if identity in visited:
                break
            visited.add(identity)
        value = current.get(key)
        if value is not None:
            return value
        parent = current.get("/Parent")
        current = parent if isinstance(parent, pikepdf.Dictionary) else None
    return None


def description_is_generic(description: str) -> bool:
    normalized = re.sub(r"[\s_.#-]+", " ", description.strip().casefold())
    if not normalized:
        return True
    if normalized in {
        "enter",
        "optional",
        "optional field",
        "required",
        "required field",
        "select",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:text|field|text field|check box|checkbox|radio button|radio|"
            r"button|choice|combo box|list box|signature)\s*)?\d+",
            normalized,
        )
    )


def widget_description(widget: pikepdf.Object) -> str:
    """Return display context, not proof that an explicit accessible name exists."""
    name = str(inherited_field_value(widget, "/T") or "").strip()
    tooltip = str(inherited_field_value(widget, "/TU") or "").strip()
    return tooltip or name


def terminal_field_dictionary(widget: pikepdf.Object) -> pikepdf.Object:
    """Return the terminal field dictionary that owns a widget.

    A merged field/widget dictionary has no /Parent and owns itself. A separate
    widget annotation's immediate /Parent is its terminal field dictionary.
    """
    if any(key in widget for key in ("/FT", "/T", "/TU")):
        return widget
    parent = widget.get("/Parent")
    return parent if isinstance(parent, pikepdf.Dictionary) else widget


def field_name(widget: pikepdf.Object) -> str:
    """Return the fully qualified field name assembled from partial /T entries."""
    current: pikepdf.Object | None = terminal_field_dictionary(widget)
    parts: list[str] = []
    visited: set[tuple[int, int]] = set()
    while isinstance(current, pikepdf.Dictionary):
        identity = current.objgen
        if identity != (0, 0):
            if identity in visited:
                break
            visited.add(identity)
        partial = str(current.get("/T") or "").strip()
        if partial:
            parts.append(partial)
        parent = current.get("/Parent")
        current = parent if isinstance(parent, pikepdf.Dictionary) else None
    return ".".join(reversed(parts))


def field_tooltip(widget: pikepdf.Object) -> str:
    """Return the explicit /TU on the terminal field dictionary."""
    owner = terminal_field_dictionary(widget)
    return str(owner.get("/TU") or "").strip()


def set_field_tooltip(widget: pikepdf.Object, description: str) -> pikepdf.Object:
    """Set the reviewed accessible name on the terminal field dictionary."""
    owner = terminal_field_dictionary(widget)
    owner[pikepdf.Name("/TU")] = pikepdf.String(description)
    return owner


def iter_page_widgets(
    pdf: pikepdf.Pdf,
) -> Iterator[tuple[int, int, pikepdf.Object]]:
    for page_number, page in enumerate(pdf.pages, start=1):
        for annotation_index, annotation in enumerate(
            page.obj.get("/Annots", [])
        ):
            if str(annotation.get("/Subtype", "")) == "/Widget":
                yield page_number, annotation_index, annotation


def _simple_value(value: object | None) -> object:
    if value is None:
        return None
    if isinstance(value, pikepdf.Array):
        return [_simple_value(item) for item in value]
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _field_count(pdf: pikepdf.Pdf) -> int:
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return 0
    count = 0
    stack = list(acroform.get("/Fields", []))
    visited: set[tuple[int, int]] = set()
    while stack:
        field = stack.pop()
        if not isinstance(field, pikepdf.Dictionary):
            continue
        identity = field.objgen
        if identity != (0, 0):
            if identity in visited:
                continue
            visited.add(identity)
        if inherited_field_value(field, "/FT") is not None:
            count += 1
        stack.extend(field.get("/Kids", []))
    return count


def _form_structure_labels(pdf: pikepdf.Pdf) -> dict[tuple[int, int], str]:
    labels: dict[tuple[int, int], str] = {}
    structure_root = pdf.Root.get("/StructTreeRoot")
    if not isinstance(structure_root, pikepdf.Dictionary):
        return labels

    def object_references(value: object) -> Iterator[pikepdf.Object]:
        if isinstance(value, pikepdf.Array):
            for child in value:
                yield from object_references(child)
        elif isinstance(value, pikepdf.Dictionary):
            if str(value.get("/Type", "")) == "/OBJR":
                referenced = value.get("/Obj")
                if isinstance(referenced, pikepdf.Dictionary):
                    yield referenced
            else:
                yield from object_references(value.get("/K"))

    def walk(value: object, visited: set[tuple[int, int]]) -> None:
        if isinstance(value, pikepdf.Array):
            for child in value:
                walk(child, visited)
            return
        if not isinstance(value, pikepdf.Dictionary):
            return
        identity = value.objgen
        if identity != (0, 0):
            if identity in visited:
                return
            visited.add(identity)
        if str(value.get("/S", "")) == "/Form":
            label = str(value.get("/Alt") or "").strip()
            for referenced in object_references(value.get("/K")):
                if referenced.objgen != (0, 0):
                    labels[referenced.objgen] = label
        walk(value.get("/K"), visited)

    walk(structure_root.get("/K"), set())
    return labels


def form_accessibility_errors(snapshot: dict[str, object]) -> list[str]:
    """Return project-policy errors that PDF/UA validators may not report."""
    errors: list[str] = []
    for widget in snapshot.get("widgets", []):
        page = int(widget.get("page", 0))
        index = int(widget.get("annotation_index", 0))
        prefix = f"page {page} widget {index}"
        tooltip = str(widget.get("tooltip", "")).strip()
        structure_label = str(widget.get("structure_label", "")).strip()
        if not tooltip:
            errors.append(f"{prefix}: terminal field has no explicit /TU accessible name")
        elif description_is_generic(tooltip):
            errors.append(
                f"{prefix}: terminal field /TU accessible name is generic: {tooltip!r}"
            )
        if not structure_label:
            errors.append(f"{prefix}: Form structure element has no /Alt label")
        elif tooltip and structure_label != tooltip:
            errors.append(
                f"{prefix}: terminal field /TU does not match its Form structure /Alt"
            )
    return errors


def form_snapshot_pdf(pdf: pikepdf.Pdf) -> dict[str, object]:
    acroform = pdf.Root.get("/AcroForm")
    structure_labels = _form_structure_labels(pdf)
    widgets: list[dict[str, object]] = []
    for page_number, annotation_index, widget in iter_page_widgets(pdf):
        owner = terminal_field_dictionary(widget)
        tooltip = field_tooltip(widget)
        name = field_name(widget)
        structure_label = structure_labels.get(widget.objgen, "")
        rect = [round(float(value), 3) for value in widget.get("/Rect", [])]
        normal = widget.get("/AP", {}).get("/N")
        if isinstance(normal, pikepdf.Dictionary) and not isinstance(
            normal, pikepdf.Stream
        ):
            appearance_states = sorted(str(key) for key in normal.keys())
        elif isinstance(normal, pikepdf.Stream):
            appearance_states = ["stream"]
        else:
            appearance_states = []
        widgets.append(
            {
                "page": page_number,
                "annotation_index": annotation_index,
                "name": name,
                "tooltip": tooltip,
                "description": tooltip or name,
                "accessible_name_source": (
                    "tooltip" if tooltip else ("field_name_fallback" if name else "missing")
                ),
                "field_owner": list(owner.objgen),
                "structure_label": structure_label,
                "field_type": str(inherited_field_value(widget, "/FT") or ""),
                "value": _simple_value(inherited_field_value(widget, "/V")),
                "default_value": _simple_value(
                    inherited_field_value(widget, "/DV")
                ),
                "field_flags": int(inherited_field_value(widget, "/Ff") or 0),
                "annotation_flags": int(widget.get("/F", 0)),
                "maximum_length": int(
                    inherited_field_value(widget, "/MaxLen") or 0
                ),
                "options": _simple_value(inherited_field_value(widget, "/Opt")),
                "rect": rect,
                "appearance_states": appearance_states,
            }
        )
    snapshot = {
        "acroform_present": isinstance(acroform, pikepdf.Dictionary),
        "xfa_present": bool(
            isinstance(acroform, pikepdf.Dictionary)
            and acroform.get("/XFA") is not None
        ),
        "field_count": _field_count(pdf),
        "widget_count": len(widgets),
        "widgets_missing_descriptions": sum(
            not str(widget["tooltip"]).strip() for widget in widgets
        ),
        "widgets_with_generic_descriptions": sum(
            bool(str(widget["tooltip"]).strip())
            and description_is_generic(str(widget["tooltip"]))
            for widget in widgets
        ),
        "widgets_missing_tooltips": sum(
            not str(widget["tooltip"]).strip() for widget in widgets
        ),
        "widgets_with_generic_tooltips": sum(
            bool(str(widget["tooltip"]).strip())
            and description_is_generic(str(widget["tooltip"]))
            for widget in widgets
        ),
        "widgets_missing_structure_labels": sum(
            not str(widget["structure_label"]).strip() for widget in widgets
        ),
        "widgets_with_mismatched_structure_labels": sum(
            bool(str(widget["tooltip"]).strip())
            and bool(str(widget["structure_label"]).strip())
            and str(widget["tooltip"]).strip()
            != str(widget["structure_label"]).strip()
            for widget in widgets
        ),
        "widgets": widgets,
    }
    snapshot["accessibility_errors"] = form_accessibility_errors(snapshot)
    return snapshot


def form_snapshot(path: Path) -> dict[str, object]:
    with pikepdf.Pdf.open(path) as pdf:
        return form_snapshot_pdf(pdf)
