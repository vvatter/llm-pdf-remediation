# Development and Experiment Log

This is the append-oriented engineering record for the project. It records what was
tried, what real PDF readers and validators did, what was changed, and why. That is
especially important here because PDF accessibility behavior is partly empirical:
machine-valid structures can still behave poorly in Acrobat, text extraction, or a
screen reader.

New entries should include the date, inputs, command or configuration, measured results,
decision, and unresolved questions. Generated build artifacts remain under `build/`;
this file retains the durable conclusions needed to reproduce or challenge a decision.

## Before 2026-07-12: Proof-of-Concept Iterations

### LLM transcript plus existing native text

The first approach retained the visible PDF and attempted to add semantic structure
around existing text. This worked unevenly. The 1996 issue contained text that could not
be copied reliably, while the 2004 issue had selectable text but incomplete or corrupt
encodings. Acrobat Read Out Loud skipped content, read some pages as only their page
number, and sometimes produced gibberish.

**Decision:** native and OCR text are evidence, not the historical transcript. The saved
reviewed plan is authoritative for compilation.

### OCRmyPDF facsimile base

OCRmyPDF was used to create a stable visible base and word geometry. The vision model
supplied the canonical words and reading order. This materially improved the 1996 issue:
selection/copying became reliable and Acrobat Read Out Loud was approximately 95%
correct. Applying the same treatment to 2004 fixed word recognition but initially
caused poor two-column traversal, missed paragraph lines, difficult page navigation,
and a file-size increase to roughly 16 MB.

**Decision:** keep OCRmyPDF in the background for appearance/geometry, but never treat
its transcript as canonical.

### Marked-content granularity experiments

Paragraph-level and larger text chunks caused Acrobat to begin paragraphs partway
through, skip later lines, or fail to expose individually selectable words. Emitting one
MCID and `/ActualText` value per word, with evidence-derived word boxes, made selection
and Read Out Loud substantially more predictable. Exact following joiners were later
stored so punctuation and whitespace were not reconstructed by appending a generic
space.

**Decision:** retain word-level MCIDs as an Acrobat compatibility profile, isolated from
the semantic plan so another compiler strategy can be tested later.

## 2026-07-12: Three-Document Golden Run

### Configuration

- Inputs: `1996_newsletter.pdf`, `2004_newsletter.pdf`, `2007_newsletter.pdf`
- Proposal: `gpt-5.6-terra`, medium reasoning
- Review: `gpt-5.6-sol`, high reasoning
- Workers: 2 per document; documents ran in parallel
- Calls: 90 model calls, all completed without API or schema failures
- Validation: exact render hashes, qpdf, structure-tree transcript, ParentTree/MCID
  invariants, and veraPDF PDF/UA-1

### Results

| Input | Pages | Preflight mode | Elements | Output size | Released |
| --- | ---: | --- | ---: | ---: | --- |
| 1996 | 9 | facsimile | 130 | 1.43 MB | yes |
| 2004 | 24 | facsimile | 291 | 11.35 MB | yes |
| 2007 | 12 | native | 222 | 1.40 MB | yes |

All three outputs preserved the selected visible rendering, passed qpdf and structure
comparison, were fully tagged, and passed veraPDF UA-1. The run took approximately 10,
31, and 17 minutes respectively; parallel wall time was approximately 31 minutes.

### Finding: native extraction duplicated text

For the facsimile outputs, canonical-token agreement with `pdftotext -raw` was 1.000
for 1996 and 0.997 for 2004, with token-count ratios of 1.000 and 0.999. The native 2007
output scored 0.665 agreement and 1.976 token-count ratio. Its original native text and
new semantic anchors were both exposed to ordinary extraction. Marking original content
as an artifact controlled the structure tree but did not remove it from `pdftotext`.

**Decision:** use facsimile as the batch-safe default, keep native mode experimental,
and add a release gate requiring token agreement at least 0.99 and token-count ratio
between 0.98 and 1.02.

### Finding: coordinate-space contract was violated

The schema and compiler described bboxes as normalized 0..1000 coordinates. Terra used
that convention, but the review prompt emphasized 612 by 792 point page dimensions and
Sol converted many canonical boxes to PDF points. Current canonical maxima around 583
by 776 confirmed the mismatch. Text placement happened to remain good because aligned
word evidence, not canonical element boxes, handled most anchor geometry. Figure and
fallback geometry remained unsafe.

**Decision:** make coordinate space explicit, constrain model output to one literal
value, normalize evidence before prompting, migrate v2 boxes as PDF points, and derive
text-element boxes from aligned word placements.

### Finding: page furniture entered the semantic stream

Printed page numbers appeared as paragraphs on 5 pages in 1996, 13 pages in 2004, and 8
pages in 2007. The 2007 footer was repeated on 10 pages. The 2004 donation form included
a long paragraph consisting only of writing dots. Five tiny decorative objects were
given the fabricated fallback alt text `Historical document image`.

**Decision:** represent artifacts explicitly, identify stable page furniture
deterministically, and never synthesize generic figure alt text. Missing meaningful alt
text is a critical review finding.

### Finding: anomaly and transformation records were noisy

The run produced 1,056 anomaly records: 693 info, 335 warning, and 28 critical. Category
names were unconstrained, and a page-level proposal/review disagreement was duplicated
onto every element, so eight affected pages generated 115 records. Model-supplied
transformations often contained semicolon-separated examples rather than applicable
source spans.

**Decision:** normalize findings into a finite category set, deduplicate them, emit one
model-disagreement record per page, collapse info entries in HTML, and derive exact
transformation spans with a reconstruction audit.

## 2026-07-13: Deterministic Safeguards (Version 0.3.0)

### Implemented

- Schema v3 coordinate-space and artifact provenance.
- Idempotent post-review refinement; raw proposal/review checkpoints remain immutable.
- Exact source/target spans for every visible-to-accessible text change.
- Critical advisory for unverified textual changes without stopping batch processing.
- Explicit page-number, repeated-furniture, writing-line, and decoration artifacts.
- No automatic `Historical document image` fallback.
- Evidence-derived normalized text-element geometry.
- Decimal PDF page labels.
- `pdftotext -raw` extraction agreement and token-count release gates.
- Sampled original-to-base visual fidelity at 72 and 150 DPI, in addition to exact
  base-to-output render equality.
- Facsimile default for automatic native candidates; native requires
  `--native-experimental`.
- Finite anomaly categories, exact deduplication, one model-disagreement record per
  page, and collapsed informational entries.
- Regression tests covering schema migration, coordinates, artifacts, transformations,
  page labels, and native extraction duplication.

### Pre-rebuild deterministic audit

Applying refinement in memory to the saved reviewed plans removed 5 semantic artifacts
from 1996, 31 from 2004, and 18 from 2007. It identified all five generic decorative
figures in 2004. Every transformation set reconstructed its accessible string exactly.

### Golden rebuild results

The existing proposal and review checkpoints were reused; no new model calls were made.
All three documents released successfully.

| Input | Semantic elements | Artifacts | Anomalies (info/warn/critical) | Extraction agreement | Output size |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1996 | 125 | 5 | 187 (128/56/3) | 1.000000 | 1.42 MB |
| 2004 | 260 | 31 | 478 (293/184/1) | 0.999888 | 11.31 MB |
| 2007 | 204 | 18 | 226 (141/85/0) | 0.999345 | 11.94 MB |

Extraction token-count ratios were 1.000000, 0.999925, and 0.999563. All three outputs
passed exact selected-base rendering, qpdf, structure-tree comparison, complete tagging,
page labels, transformation reconstruction, extraction compatibility, plan approval,
and veraPDF PDF/UA-1.

The artifact inventory now contains 26 printed page numbers, 22 repeated furniture
items, 5 decorative graphics, and 1 writing-line region. The five generic figure
fallbacks are gone. All plan boxes declare `normalized_0_1000` and fall within the page
bounds.

Anomaly volume fell from 1,056 to 891. Informational findings fell from 693 to 562,
warnings from 335 to 325, and critical findings from 28 to 4. The remaining critical
items are substantive rather than pipeline noise: two 1996 cross-column reading-order
notes, one 1996 authored-spelling decision (`Baccalaureatte`), and one 2004 unapproved
change from `p–groups` to `p groups`.

### Original-to-base visual fidelity

The exact render gate compares the selected base with the remediated output. A separate
sampled comparison now measures the raster base against the archival source:

| Input | Aggregate mean at 72/150 DPI | Maximum page mean | Maximum page material fraction | Result |
| --- | ---: | ---: | ---: | --- |
| 1996 | 0.039109 / 0.032221 | 0.047738 | 0.218179 | pass |
| 2004 | 0.024828 / 0.018209 | 0.036217 | 0.195418 | pass |
| 2007 | 0.025969 / 0.022192 | 0.038722 | 0.208611 | pass |

The gate permits each page at most 0.05 mean normalized channel difference and 0.25 of
sampled channels differing by more than 16 levels. These thresholds are empirical and
should be revisited as the corpus grows; aggregate and per-page raw metrics are retained
in each validation report.

### Cost and remaining tradeoffs

The safer 2007 facsimile fixed its prior 1.976 extraction token ratio, but increased the
output from 1.40 MB in experimental native mode to 11.94 MB. Its OCR base is 11.16 MB
versus a 0.55 MB source. The 2004 OCR base similarly accounts for most of its 11.31 MB
output. Correctness and reader behavior remain the priority, so no image-quality or font
optimization was mixed into this release.

### Recommended next experiments

1. Build the write-back visual reviewer. The golden run now isolates four critical
   decisions and makes informational findings collapsible, but 325 warnings still need
   efficient human triage and durable approval metadata.
2. Run the institutional acceptance matrix with NVDA plus Acrobat Reader on Windows,
   Acrobat Reflow, continuous reading, heading/figure navigation, selection/copying,
   page changes, 400% zoom, and representative pages 1, 5, 12–13, 17, and 23.
3. Run a controlled facsimile-size experiment, especially on 2007. Compare OCR render
   DPI, image codecs, font subsetting, and mixed raster strategies while requiring every
   current fidelity, extraction, structure, Acrobat, and veraPDF result to remain equal
   or better.
4. Add the document/article graph and the newsletter-relevant roles `Caption`, lists,
   `TOC`, and `Formula`; then replace page-wide alignment with region/line alignment.
5. Treat native preservation as a longer-term compiler project: tag or replace existing
   text objects instead of layering duplicate anchors. Keep it experimental until it
   passes the same extraction and reader-behavior gates as facsimile mode.
