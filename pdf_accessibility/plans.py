from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import DocumentPlan, ReviewStatus, SCHEMA_VERSION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_sha256(plan: DocumentPlan) -> str:
    content = plan.model_dump_json(exclude_none=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def load_document_plan(path: Path, source: Path | None = None) -> DocumentPlan:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") == SCHEMA_VERSION:
        return DocumentPlan.model_validate(raw)

    backup = path.with_suffix(".legacy.json")
    if not backup.exists():
        backup.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    raw["schema_version"] = SCHEMA_VERSION
    raw["source_sha256"] = sha256_file(source) if source and source.exists() else ""
    raw["source_page_count"] = len(raw.get("pages", []))
    raw["review_status"] = ReviewStatus.LEGACY_UNREVIEWED.value
    raw["plan_revision"] = 1
    for page in raw.get("pages", []):
        page["review_status"] = ReviewStatus.LEGACY_UNREVIEWED.value
        for element in page.get("elements", []):
            element["review_status"] = ReviewStatus.LEGACY_UNREVIEWED.value

    plan = DocumentPlan.model_validate(raw)
    path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return plan


def write_document_plan(plan: DocumentPlan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
