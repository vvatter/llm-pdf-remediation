from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from .compiler import compile_tagged_pdf, merge_column_continuations
from .extract import extract_page_packets
from .planner import build_document_plan
from .validate import write_validation_report


def _write_ambiguities(plan, path: Path, threshold: float) -> int:
    records: list[dict[str, object]] = []
    for page in plan.pages:
        for message in page.page_ambiguities:
            records.append({"page": page.page_number, "type": "page", "message": message})
        for index, element in enumerate(page.elements):
            if element.ambiguity or element.confidence < threshold:
                records.append(
                    {
                        "page": page.page_number,
                        "element": index,
                        "role": element.role.value,
                        "confidence": element.confidence,
                        "message": element.ambiguity or "low-confidence semantic decision",
                        "chosen_text": element.text[:240],
                    }
                )
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_text(text, encoding="utf-8")
    return len(records)


def _ensure_ocr_base(source: Path, output: Path, jobs: int) -> None:
    if output.exists():
        return
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError("--ocr requires the ocrmypdf command")
    temp_dir = output.parent / "ocr-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["TMPDIR"] = str(temp_dir)
    subprocess.run(
        [
            "ocrmypdf",
            "--force-ocr",
            "--output-type",
            "pdf",
            "--optimize",
            "3",
            "--jpeg-quality",
            "88",
            "--png-quality",
            "90",
            "--jobs",
            str(max(1, jobs)),
            "--oversample",
            "300",
            str(source),
            str(output),
        ],
        check=True,
        env=environment,
    )


def remediate(args: argparse.Namespace) -> int:
    source = args.input.resolve()
    stem = source.stem
    workdir = args.output_dir.resolve() / stem
    workdir.mkdir(parents=True, exist_ok=True)
    if args.ocr:
        base_pdf = workdir / f"{stem}.ocr-base.pdf"
        _ensure_ocr_base(source, base_pdf, args.ocr_jobs)
    else:
        base_pdf = args.base_pdf.resolve() if args.base_pdf else source
    plan_path = workdir / f"{stem}.plan.json"
    output_pdf = workdir / f"{stem}.accessible.pdf"
    ambiguity_path = workdir / f"{stem}.ambiguities.jsonl"
    validation_path = workdir / f"{stem}.validation.json"

    packets = extract_page_packets(source, dpi=args.dpi)
    if args.max_pages:
        packets = packets[: args.max_pages]
    plan = build_document_plan(
        source=source,
        packets=packets,
        model=args.model,
        checkpoint_path=plan_path,
        workers=args.workers,
    )
    continuation_records = merge_column_continuations(
        plan, base_pdf, geometry_source=source
    )
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    ambiguity_count = _write_ambiguities(plan, ambiguity_path, args.ambiguity_threshold)
    geometry_sources = compile_tagged_pdf(
        base_pdf, output_pdf, plan, geometry_source=source
    )
    report = write_validation_report(base_pdf, output_pdf, validation_path)

    print(f"output: {output_pdf}")
    print(f"plan: {plan_path}")
    print(f"ambiguities: {ambiguity_count} ({ambiguity_path})")
    print(f"column continuations merged: {len(continuation_records)}")
    print(
        "geometry pages: "
        f"original={geometry_sources.count('original')}, ocr={geometry_sources.count('ocr')}"
    )
    print(f"visual match: {report['visual_match']}")
    print(f"qpdf valid: {report['qpdf_ok']}")
    return 0 if report["visual_match"] and report["qpdf_ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an LLM-planned, tagged accessible PDF"
    )
    parser.add_argument("input", type=Path)
    base_group = parser.add_mutually_exclusive_group()
    base_group.add_argument(
        "--base-pdf",
        type=Path,
        help="PDF whose unchanged pages receive the tags (for example, an OCRmyPDF output)",
    )
    base_group.add_argument(
        "--ocr",
        action="store_true",
        help="create or reuse an OCRmyPDF raster base before adding corrected text and tags",
    )
    parser.add_argument("--ocr-jobs", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=Path("build"))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"))
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--ambiguity-threshold", type=float, default=0.8)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(remediate(args))


if __name__ == "__main__":
    main()
