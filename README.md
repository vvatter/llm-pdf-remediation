# LLM-First PDF Accessibility Remediation

> **Development disclosure:** This project is vibe-coded using **ChatGPT-5.6 Sol
> (xhigh)**. Its architecture, implementation, tests, experiments, and documentation
> are being developed through human-directed collaboration with that model.

## What This Does

The goal is to make old PDFs much more accessible without changing how their pages
look. The tool uses AI models to read each page, correct mistakes left by automatic text
recognition, identify headings, paragraphs, and meaningful images, and decide the order
in which everything should be read. It then adds the improved text and organization
behind the original page.

The resulting PDF should be easier to read aloud, search, select, copy, and navigate
with accessibility software. The finished file is checked automatically before it is
released. Automated checks are useful, but they do not replace testing with the PDF
reader and assistive technology used by the intended audience.

The first completed collection is a set of University of Florida mathematics
newsletters. The project is not limited to newsletters; it is intended for old PDFs
whose original publishing files are no longer available.

## Before You Start

You need an **OpenAI API key** to run the remediation process. The tool sends page
images and supporting text to OpenAI models. Set the key in the `OPENAI_API_KEY`
environment variable, and do not save it in this repository.

The recommended way to use the project is to clone it into a local directory, open that
directory in **Codex** or **Claude Code**, and ask the coding assistant to install the
requirements and run the tool on your PDFs:

```sh
git clone https://github.com/vvatter/llm-pdf-remediation.git
cd llm-pdf-remediation
```

In Git terminology, **clone** is the right operation for making the local copy. A fork
is only needed if you want a separate GitHub repository under your own account. Pulling
updates happens after the repository has been cloned.

## How It Works

The first AI model proposes the page text, organization, image descriptions, and
reading order. A second AI model checks that work while looking at the original page.
The approved result is saved, and ordinary Python code builds the PDF from it. The
models never write PDF instructions directly.

The production workflow is unattended. Uncertain readings are recorded without
pausing the run, and the second model must still choose a result. Human editing is not a
pipeline stage, although real-reader testing remains an important final check.

See [APPROACH.md](APPROACH.md) for the detailed architecture, trust model, PDF
construction strategy, validation gates, and known limitations.

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

`.venv` is a conventional name for a project-local Python environment. It is created
from the Python installation on the current machine, is not committed to Git, and can
be deleted and recreated at any time.

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
- `*.plan.json`: schema-v4 canonical plan used by the compiler
- `*.draft.pdf`: tagged draft without a PDF/UA declaration
- `*.accessible.pdf`: published only after every machine gate passes
- `*.validation.json`: render, qpdf, structure-tree, and veraPDF results
- `*.anomalies.jsonl` and `*.anomalies.html`: nonblocking review advisories
- `*.wcag.json`: per-criterion WCAG 2.1 AA evidence matrix
- `*.manifest.json`: hashes, models, prompts, tools, font, and compiler strategy

Old schema-v1 through schema-v3 plans migrate automatically. The original is preserved as
`*.plan.legacy.json`. Existing reviewed or manually modified canonical plans are not
overwritten unless a force option is supplied.

## Current Status

All nine newsletters in the original project corpus have released accessible outputs:
1996, 1997, 1998, 2004, 2005, 2007, 2008, 2009, and 2010. Every output passed the
project's machine checks and veraPDF's PDF/UA-1 checks. The 1996, 2004, and 2007 issues
also received successful Acrobat Read Out Loud, clicking, selection, and reading-order
spot checks. The remaining six still need issue-by-issue testing in a real PDF reader.

## Release Semantics

The compiler first creates an undeclared draft. It then verifies exact rendered-page
equality, qpdf integrity, every MCID and ParentTree relationship, and an exact
element-by-element structure-tree transcript. It also proves that exact transformation
spans reconstruct the accessible text, checks that every visual block has one ordered
flow owner and block-local geometry, and requires `pdftotext -raw` to agree with the
semantic transcript without duplication. The selected base is compared with the
original at 72 and 150 DPI, and the final PDF must match that base exactly. Only a
temporary candidate receives
`pdfuaid:part=1`. The final accessible filename is published only if veraPDF PDF/UA-1
also passes.

Model findings, including critical findings, are logged but do not interrupt a batch.
They remain visible in the anomaly report. NVDA or JAWS testing, Acrobat reflow, and
manual WCAG checks are still needed as external evidence for an institutional
conformance claim; they do not create an interactive remediation or approval stage.

See [DEVELOG.md](DEVELOG.md) for the experiment log, measured results, decisions, and
open questions that explain why the current implementation behaves this way.

## License

GNU Affero General Public License v3.0 or later. Third-party tools retain their own
licenses.
