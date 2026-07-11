# LLM-First PDF Accessibility Remediation

See [APPROACH.md](APPROACH.md) for a detailed description of the architecture, design
decisions, validation strategy, tradeoffs, and lessons from the proof of concept.

This project uses a vision-capable OpenAI model to transcribe and order historical
fixed-layout document pages, then writes a tagged PDF without changing the selected
visible page base. The included proof corpus consists of departmental newsletters, but
the planning, alignment, compilation, and validation pipeline is intended to be general.

The proof of concept preserves each visible page as an artifact and adds ordered,
invisible Unicode text runs in marked-content sequences. The accessibility font is an
embedded Noto Sans TrueType font with explicit glyph and `/ToUnicode` maps. Long semantic
elements remain paragraph-level tags whose ordered content references point to individual
word MCIDs. Each word carries the exact model-approved transcript through `/ActualText`.
It currently supports headings, paragraphs, figures, alternative text, reading order,
document language, title metadata, ambiguity logging, and conservative merging of
paragraphs that continue from the bottom of a left column to the top of a right column.

The default model is `gpt-5.4-mini`; set `OPENAI_MODEL` or pass `--model` to override it.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
source ~/.zshrc  # or otherwise export OPENAI_API_KEY
```

## Run

```sh
.venv/bin/remediate-pdf src/96_newsletter.pdf --ocr
.venv/bin/remediate-pdf src/2004_newsletter.pdf --ocr
```

`--ocr` creates a visually equivalent raster base with OCRmyPDF, uses its word boxes to
position the corrected LLM transcript, and suppresses Tesseract's uncorrected text form.
The base is cached under `build/<source-name>/` for subsequent runs. This path is used for
all newsletters, including PDFs that already contain selectable legacy text.
OCRmyPDF's image optimization is enabled to limit the size increase from rasterization;
lossy JBIG2 character substitution is deliberately not enabled.

When the original PDF already has usable native text, the compiler compares its word
geometry with OCRmyPDF's geometry page by page and uses the better-aligned source. Every
marked-content sequence also carries the canonical LLM `/ActualText`, so assistive
technology does not need to reconstruct words from fitted glyph positions.

After planning, the pipeline uses those same word boxes to merge incomplete cross-column
paragraph fragments. Each automatic merge is recorded in the ambiguity log and does not
require interactive review.

The equivalent explicit two-step form is:

```sh
mkdir -p build/ocr-tmp build/96_newsletter
TMPDIR="$PWD/build/ocr-tmp" ocrmypdf \
  --force-ocr --output-type pdf --optimize 3 --jpeg-quality 88 --png-quality 90 \
  --jobs 4 --oversample 300 \
  src/96_newsletter.pdf build/96_newsletter/96_newsletter.ocr-base.pdf
.venv/bin/remediate-pdf src/96_newsletter.pdf \
  --base-pdf build/96_newsletter/96_newsletter.ocr-base.pdf
```

`A11Y_FONT_PATH` can point to another open TrueType font when Noto Sans or DejaVu Sans
is not installed in one of the compiler's standard locations.

For a short API and compiler test:

```sh
.venv/bin/remediate-pdf src/96_newsletter.pdf --max-pages 1
```

Outputs are checkpointed under `build/<source-name>/`. Re-running resumes from the
existing plan. Delete the plan JSON to request fresh model results.

The validation report checks qpdf syntax and exact rendered-pixel equality against the
selected base PDF. It does not replace PAC, veraPDF, Acrobat, or human screen-reader
testing.

## Current proof-of-concept results

Both example newsletters have complete page plans and generated PDFs under `build/`.
Their rendered page pixels exactly match their OCR bases, qpdf reports no syntax errors,
and all semantic elements carry `/ActualText` or `/Alt` payloads. Pages from both OCR
bases were also visually inspected against their sources.

An open-source veraPDF 1.30.2 PDF/UA-1 run is saved as `build/verapdf-ua1.json`:

- `96_newsletter`: compliant; 106 rules pass and zero checks fail.
- `2004_newsletter`: compliant; 106 rules pass and zero checks fail.

Acrobat Read Out Loud and human screen-reader testing remain separate acceptance checks.

## License

This project is licensed under the GNU Affero General Public License v3.0 or later.
Third-party dependencies and external tools retain their own licenses.
