from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .compiler import compile_tagged_pdf
from .extract import extract_page_packets
from .models import RemediationMode
from .planner import build_document_plan
from .plans import load_document_plan, write_document_plan
from .preflight import inspect_pdf, write_preflight
from .refine import refine_document_plan
from .reporting import build_manifest, wcag_evidence, write_anomaly_reports
from .validate import release_pdfua, validate_output, write_validation_report


def _ensure_ocr_base(source: Path, output: Path, jobs: int) -> None:
    if output.exists():
        return
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError("facsimile mode requires the ocrmypdf command")
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


def _paths(source: Path, output_dir: Path) -> dict[str, Path]:
    workdir = output_dir.resolve() / source.stem
    stem = source.stem
    return {
        "workdir": workdir,
        "preflight": workdir / f"{stem}.preflight.json",
        "base": workdir / f"{stem}.ocr-base.pdf",
        "plan": workdir / f"{stem}.plan.json",
        "draft": workdir / f"{stem}.draft.pdf",
        "output": workdir / f"{stem}.accessible.pdf",
        "validation": workdir / f"{stem}.validation.json",
        "anomalies": workdir / f"{stem}.anomalies.jsonl",
        "anomaly_html": workdir / f"{stem}.anomalies.html",
        "wcag": workdir / f"{stem}.wcag.json",
        "manifest": workdir / f"{stem}.manifest.json",
    }


def _apply_native_policy(report, native_experimental: bool) -> None:
    if report.selected_mode != RemediationMode.NATIVE or native_experimental:
        return
    if report.requested_mode == RemediationMode.NATIVE:
        report.selected_mode = RemediationMode.UNSUPPORTED
        report.reasons.append(
            "Native mode is experimental because existing text may duplicate semantic anchors; "
            "rerun with --native-experimental to accept that risk."
        )
    else:
        report.selected_mode = RemediationMode.FACSIMILE
        report.reasons.append(
            "Batch-safe policy changed automatic native mode to facsimile; native mode remains "
            "available with --native-experimental."
        )


def preflight_command(args: argparse.Namespace) -> int:
    source = args.input.resolve()
    report = inspect_pdf(source, RemediationMode(args.mode))
    _apply_native_policy(report, args.native_experimental)
    path = _paths(source, args.output_dir)["preflight"]
    write_preflight(report, path)
    print(f"mode: {report.selected_mode.value}")
    for reason in report.reasons:
        print(f"reason: {reason}")
    print(f"report: {path}")
    return 1 if report.selected_mode == RemediationMode.UNSUPPORTED else 0


def run_command(args: argparse.Namespace) -> int:
    source = args.input.resolve()
    paths = _paths(source, args.output_dir)
    paths["workdir"].mkdir(parents=True, exist_ok=True)
    requested_mode = RemediationMode.FACSIMILE if args.ocr else RemediationMode(args.mode)
    preflight = inspect_pdf(source, requested_mode)
    _apply_native_policy(preflight, args.native_experimental)
    write_preflight(preflight, paths["preflight"])
    if preflight.selected_mode == RemediationMode.UNSUPPORTED:
        raise RuntimeError("preflight selected unsupported mode: " + "; ".join(preflight.reasons))
    if preflight.selected_mode == RemediationMode.PASS_THROUGH:
        report = {
            "source": str(source),
            "pass_through": True,
            "verapdf_pdfua_ok": preflight.pdfua_valid,
            "released": True,
            "note": "The input PDF was not modified.",
        }
        paths["validation"].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"pass-through: {source}")
        print(f"validation: {paths['validation']}")
        return 0

    if args.base_pdf:
        base_pdf = args.base_pdf.resolve()
    elif preflight.selected_mode == RemediationMode.FACSIMILE:
        base_pdf = paths["base"]
        _ensure_ocr_base(source, base_pdf, args.ocr_jobs)
    else:
        base_pdf = source

    packets = extract_page_packets(source, dpi=args.dpi, evidence_pdf=base_pdf)
    if args.max_pages:
        packets = packets[: args.max_pages]
    planner_model = args.model or args.planner_model
    plan = build_document_plan(
        source=source,
        packets=packets,
        checkpoint_path=paths["plan"],
        planner_model=planner_model,
        reviewer_model=args.review_model,
        planner_reasoning=args.planner_reasoning,
        reviewer_reasoning=args.review_reasoning,
        workers=args.workers,
        force_replan=args.force_replan,
        force_review=args.force_review,
    )
    refine_document_plan(source, plan)
    geometry_sources = compile_tagged_pdf(
        base_pdf, paths["draft"], plan, geometry_source=source, declare_pdfua=False
    )
    write_document_plan(plan, paths["plan"])
    draft_report = validate_output(
        base_pdf, paths["draft"], plan, reference_source=source
    )
    if args.max_pages:
        validation = dict(draft_report)
        validation.update(
            {
                "verapdf_pdfua_ok": None,
                "released": False,
                "release_note": "Partial --max-pages runs produce an undeclared draft only.",
            }
        )
    else:
        if len(plan.pages) != preflight.page_count:
            raise RuntimeError(
                f"canonical plan contains {len(plan.pages)} of {preflight.page_count} source pages"
            )
        if paths["output"].exists():
            shutil.move(paths["output"], paths["output"].with_suffix(".previous.pdf"))
        validation = release_pdfua(
            base_pdf,
            paths["draft"],
            paths["output"],
            plan,
            reference_source=source,
            source_metadata=preflight.source_metadata,
        )
    validation["draft_validation"] = draft_report
    paths["validation"].write_text(json.dumps(validation, indent=2) + "\n", encoding="utf-8")

    anomalies = write_anomaly_reports(
        plan,
        packets,
        paths["anomalies"],
        paths["anomaly_html"],
        args.ambiguity_threshold,
        paths["workdir"] / "pages",
    )
    wcag = wcag_evidence(plan, preflight.selected_mode, validation)
    paths["wcag"].write_text(json.dumps(wcag, indent=2) + "\n", encoding="utf-8")
    manifest = build_manifest(
        source,
        plan,
        preflight,
        packets,
        planner_model,
        args.review_model,
        args.planner_reasoning,
        args.review_reasoning,
        validation,
        paths["workdir"] / "pages",
        geometry_sources,
    )
    paths["manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"draft: {paths['draft']}")
    print(f"plan: {paths['plan']}")
    print(f"anomalies: {len(anomalies)} ({paths['anomaly_html']})")
    print(f"validation: {paths['validation']}")
    if validation["released"]:
        print(f"output: {paths['output']}")
        return 0
    print("release withheld: machine gates did not all pass; inspect the draft and validation report")
    return 1


def validate_command(args: argparse.Namespace) -> int:
    plan = load_document_plan(args.plan.resolve())
    source = args.source.resolve() if args.source else args.pdf.resolve()
    report = write_validation_report(source, args.pdf.resolve(), args.report.resolve(), plan)
    print(f"structure matches plan: {report['structure_matches_plan']}")
    print(f"extraction compatible: {report['extraction_compatible']}")
    print(f"report: {args.report.resolve()}")
    return 0 if all(
        [
            report["qpdf_ok"],
            report["structure_matches_plan"],
            report["transformations_valid"],
            report["extraction_compatible"],
        ]
    ) else 1


def report_command(args: argparse.Namespace) -> int:
    workdir = args.workdir.resolve()
    plan_path = next(workdir.glob("*.plan.json"))
    preflight_path = next(workdir.glob("*.preflight.json"))
    preflight_data = json.loads(preflight_path.read_text(encoding="utf-8"))
    source = Path(preflight_data["source"])
    plan = load_document_plan(plan_path, source)
    packets = extract_page_packets(source, dpi=args.dpi)
    stem = plan_path.name.removesuffix(".plan.json")
    records = write_anomaly_reports(
        plan,
        packets,
        workdir / f"{stem}.anomalies.jsonl",
        workdir / f"{stem}.anomalies.html",
        args.ambiguity_threshold,
        workdir / "pages",
    )
    print(f"anomalies: {len(records)} ({workdir / f'{stem}.anomalies.html'})")
    return 0


def _add_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RemediationMode if mode != RemediationMode.UNSUPPORTED],
        default=RemediationMode.AUTO.value,
    )
    parser.add_argument(
        "--native-experimental",
        action="store_true",
        help="allow native mode despite possible duplicate ordinary text extraction",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLM-first accessibility remediation for fixed-layout PDFs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("preflight", help="classify a PDF before remediation")
    _add_input_options(preflight_parser)
    preflight_parser.set_defaults(handler=preflight_command)

    run_parser = subparsers.add_parser("run", help="plan, compile, validate, and report")
    _add_input_options(run_parser)
    base_group = run_parser.add_mutually_exclusive_group()
    base_group.add_argument("--base-pdf", type=Path)
    base_group.add_argument("--ocr", action="store_true", help=argparse.SUPPRESS)
    run_parser.add_argument("--ocr-jobs", type=int, default=4)
    run_parser.add_argument("--planner-model", default=os.getenv("OPENAI_PLANNER_MODEL", "gpt-5.6-terra"))
    run_parser.add_argument("--review-model", default=os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.6-sol"))
    run_parser.add_argument("--model", default=None, help="deprecated alias for --planner-model")
    run_parser.add_argument("--planner-reasoning", choices=["low", "medium", "high"], default="medium")
    run_parser.add_argument("--review-reasoning", choices=["low", "medium", "high"], default="high")
    run_parser.add_argument("--dpi", type=int, default=150)
    run_parser.add_argument("--workers", type=int, default=2)
    run_parser.add_argument("--max-pages", type=int, default=None)
    run_parser.add_argument("--ambiguity-threshold", type=float, default=0.8)
    run_parser.add_argument("--force-replan", action="store_true")
    run_parser.add_argument("--force-review", action="store_true")
    run_parser.set_defaults(handler=run_command)

    validate_parser = subparsers.add_parser("validate", help="compare a tagged PDF with a canonical plan")
    validate_parser.add_argument("pdf", type=Path)
    validate_parser.add_argument("--plan", type=Path, required=True)
    validate_parser.add_argument("--source", type=Path)
    validate_parser.add_argument("--report", type=Path, default=Path("validation.json"))
    validate_parser.set_defaults(handler=validate_command)

    report_parser = subparsers.add_parser("report", help="regenerate anomaly reports for a work directory")
    report_parser.add_argument("workdir", type=Path)
    report_parser.add_argument("--dpi", type=int, default=120)
    report_parser.add_argument("--ambiguity-threshold", type=float, default=0.8)
    report_parser.set_defaults(handler=report_command)
    return parser


def main() -> None:
    argv = sys.argv[1:]
    commands = {"preflight", "run", "validate", "report"}
    if argv and argv[0] not in commands and argv[0] not in {"-h", "--help"}:
        print("warning: direct invocation is deprecated; use 'remediate-pdf run INPUT'", file=sys.stderr)
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    try:
        status = args.handler(args)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        status = 2
    raise SystemExit(status)


if __name__ == "__main__":
    main()
