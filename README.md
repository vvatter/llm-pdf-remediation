# LLM-First PDF Accessibility Remediation

> **Development disclosure:** This project is vibe-coded using **ChatGPT-5.6 Sol
> (xhigh)**. Its architecture, implementation, tests, experiments, and documentation
> are being developed through human-directed collaboration with that model.

This project adds a deterministic semantic layer to visually fixed archival PDFs. A
vision model proposes a transcription and reading order, a second model reviews every
page, and ordinary Python code owns PDF structure, fonts, MCIDs, metadata, validation,
and serialization. The original visible page is not redesigned.

The current corpus is departmental newsletters, but the pipeline is organized around
fixed-layout PDFs rather than newsletter-specific file formats. See
[APPROACH.md](APPROACH.md) for the architecture, trust model, validation gates, and known
limitations.

## Requirements

- Python 3.11 or later
- `qpdf`
- `veraPDF`
- Poppler's `pdftotext`
- `ocrmypdf` and Tesseract for facsimile mode
- `OPENAI_API_KEY`
- An open TrueType font such as Noto Sans or DejaVu Sans

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
source ~/.zshrc  # or otherwise export OPENAI_API_KEY
```

The default model pair is deliberately fixed:

- Proposal: `gpt-5.6-terra`, medium reasoning
- Independent review: `gpt-5.6-sol`, high reasoning

Override these with `--planner-model`, `--review-model`, or the
`OPENAI_PLANNER_MODEL` and `OPENAI_REVIEW_MODEL` environment variables. There is no
automatic fallback to another model.

## Commands

Preflight classifies pass-through, native-preserving, facsimile, or unsupported mode
and saves its evidence. Automatic native candidates use facsimile mode by default until
native content can be tagged without duplicating ordinary text extraction:

```sh
.venv/bin/remediate-pdf preflight src/document.pdf
```

Run the complete pipeline:

```sh
.venv/bin/remediate-pdf run src/document.pdf
```

Force facsimile mode when the preflight result has been reviewed. Native mode is an
explicit experimental path:

```sh
.venv/bin/remediate-pdf run src/document.pdf --mode facsimile
.venv/bin/remediate-pdf run src/document.pdf --mode native --native-experimental
```

Re-run model stages explicitly:

```sh
.venv/bin/remediate-pdf run src/document.pdf --force-review
.venv/bin/remediate-pdf run src/document.pdf --force-replan
```

Validate an existing candidate against its canonical plan or regenerate the read-only
anomaly report:

```sh
.venv/bin/remediate-pdf validate build/document/document.draft.pdf \
  --plan build/document/document.plan.json --source src/document.pdf
.venv/bin/remediate-pdf report build/document
```

The old direct invocation remains available with a deprecation warning. `--ocr` is a
compatibility alias for `--mode facsimile`.

## Outputs

Each input receives a work directory under `build/` containing:

- `*.preflight.json`: source classification and font/text evidence
- `pages/*.evidence.json`: native and OCR evidence
- `pages/*.proposal.json`: immutable Terra proposal checkpoint
- `pages/*.review.json`: immutable Sol decision checkpoint
- `*.plan.json`: schema-v3 canonical plan used by the compiler
- `*.draft.pdf`: tagged draft without a PDF/UA declaration
- `*.accessible.pdf`: published only after every machine gate passes
- `*.validation.json`: render, qpdf, structure-tree, and veraPDF results
- `*.anomalies.jsonl` and `*.anomalies.html`: nonblocking review advisories
- `*.wcag.json`: per-criterion WCAG 2.1 AA evidence matrix
- `*.manifest.json`: hashes, models, prompts, tools, font, and compiler strategy

Old schema-v1 and schema-v2 plans migrate automatically. The original is preserved as
`*.plan.legacy.json`. Existing reviewed or manually modified canonical plans are not
overwritten unless a force option is supplied.

## Release Semantics

The compiler first creates an undeclared draft. It then verifies exact rendered-page
equality, qpdf integrity, every MCID and ParentTree relationship, and an exact
element-by-element structure-tree transcript. It also proves that exact transformation
spans reconstruct the accessible text and requires `pdftotext -raw` to agree with the
semantic transcript without duplication. The selected base is compared with the
original at 72 and 150 DPI, and the final PDF must match that base exactly. Only a
temporary candidate receives
`pdfuaid:part=1`. The final accessible filename is published only if veraPDF PDF/UA-1
also passes.

Model findings, including critical findings, are logged but do not interrupt a batch.
They remain visible in the anomaly report. Human approval, NVDA or JAWS testing, Acrobat
reflow, and manual WCAG checks are still needed for an institutional conformance claim.

See [DEVELOG.md](DEVELOG.md) for the experiment log, measured results, decisions, and
open questions that explain why the current implementation behaves this way.

## License

GNU Affero General Public License v3.0 or later. Third-party tools retain their own
licenses.
