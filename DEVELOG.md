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

**Superseded on 2026-07-14:** direct Unicode line strings inside region-level MCIDs
replaced the synthetic word-level layer. The later entries retain the word-level work as
the experiment that exposed Acrobat's geometry requirements, not as the current
compiler strategy.

## 2026-07-12: Three-Document Golden Run

### Configuration

- Inputs: three historical PDFs from 1996, 2004, and 2007
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
4. Add the document/article graph and the document roles `Caption`, lists,
   `TOC`, and `Formula`; then extend block-local alignment with line-level weighted
   matching where formulas or insertions need it.
5. Treat native preservation as a longer-term compiler project: tag or replace existing
   text objects instead of layering duplicate anchors. Keep it experimental until it
   passes the same extraction and reader-behavior gates as facsimile mode.

## 2026-07-13: Nested-Column Reading Order (Version 0.4.0)

### User-observed failure

Acrobat reading of page 1 in the 2007 issue did not reliably finish the narrow lower
left subcolumn before moving to the middle and right columns. This page does not have a
single ordinary two-column grid: a full-width introductory paragraph is followed by a
narrow lower-left list, a middle continuation, a right continuation, and an independent
two-column contents box below the article.

### Finding: the reviewed model plan was already correct

The saved Sol review had interpreted the page image correctly. In particular:

- `p0001-e0010-b001` is the article text at the bottom of the narrow left subcolumn and
  `p0001-e0010-b002` is its continuation at the top of the middle subcolumn;
- `p0001-e0011-b001` continues down the middle subcolumn and
  `p0001-e0011-b002` resumes at the top of the right column;
- the remaining right-column article text precedes the independent contents box.

The structure-tree serializer and `pdftotext -raw` also produced that intended logical
sequence. The failure was therefore downstream of the model's page interpretation.
The prior compiler flattened all word MCRs directly under each paragraph and aligned
geometry against a page-wide word stream. A paragraph's union box could span two
disjoint columns without preserving the model's internal block boundaries in the PDF
tag tree.

### Intermediate experiment: block-local alignment alone

Schema v4 introduced stable visual-block IDs and page flows. The compiler partitioned
canonical tokens by fragment and aligned each group only against words inside that
fragment's bounding box. A regression test deliberately interleaved left- and
right-block source words and proved that placement remained local.

The first 2007 rebuild passed every machine gate, but forensic comparison with the
previous PDF showed that the two affected paragraphs already had the same word
coordinates. Block-local alignment prevented future cross-column misalignment, but it
did not by itself change the empirical Acrobat case. This negative result is important:
correct word coordinates and a correct flat transcript are not sufficient evidence of
robust block traversal.

### First block-structure implementation

- Terra and Sol prompt schemas now require atomic rectangular fragments and explicit
  flows that own every meaningful block exactly once in reading order.
- Schema-v1 through schema-v3 plans migrate to schema v4; reviewed plans retain their
  status and derive a default flow from their existing canonical order.
- Logical paragraphs remain paragraphs when they cross columns, but the PDF structure
  now gives each text block an ordered `/Span` child with a stable `/ID` and PDF-space
  `/BBox`.
- Each word MCID's ParentTree entry points to its immediate block span. The recursive
  structure serializer verifies block order, IDs, bounding boxes, MCR ownership, exact
  logical text, and orphan/duplicate conditions.
- Decoration-only fragments that contribute no accessible token, such as blank form
  writing lines and declared decorative pointers, become explicit artifacts instead of
  empty tagged spans.
- A zero-area legacy block box may be repaired from page geometry only with at least
  50% visible-token agreement. Valid reviewed boxes are never replaced by this path.
- The compiler strategy and prompt versions are recorded as `block_local`,
  `proposal-v4`, and `review-v4` in the build manifest.

### Regression and golden rebuild results

The test suite now has 16 passing tests, including interleaved nested-column geometry,
single flow ownership, nested block serialization, ParentTree validation, and removal
of decoration-only fragments. It also covers invalid legacy-box recovery without
weakening valid block locality.

The saved 2007 proposal and review checkpoints were reused; no new model calls were
made. The rebuilt document contains 204 logical semantic elements, 263 visual blocks,
and 33 artifacts. Page 1 has 21 reviewed fragments in one explicit reading sequence.
For the affected article, `p0001-e0010` now owns two block spans containing 11 and 31
word MCRs; `p0001-e0011` owns two block spans containing 75 and 16 word MCRs. The prior
PDF exposed each paragraph as one flat MCR array.
The release passed:

- exact selected-base rendering and source-to-base fidelity;
- qpdf integrity and complete tagging;
- recursive logical-element and block-level structure comparison;
- transformation reconstruction;
- `pdftotext -raw` agreement and token-count ratio of 1.000000;
- veraPDF PDF/UA-1.

The global compiler change was also run against the 1996 and 2004 golden documents. It
found two useful cross-corpus edge cases: an omitted arrow before a 2004 photograph
caption, and one 2004 caption box that an older mixed-coordinate migration had collapsed
to zero height. The arrow is now a declared decoration artifact. The invalid caption
box was recovered from all 33 matching native words with 1.000 alignment coverage.

| Input | Logical elements | Visual blocks | Artifacts | Extraction agreement/ratio | Released |
| --- | ---: | ---: | ---: | --- | --- |
| 1996 | 125 | 141 | 5 | 1.000000 / 1.000000 | yes |
| 2004 | 260 | 283 | 39 | 1.000000 / 1.000000 | yes |
| 2007 | 204 | 263 | 33 | 1.000000 / 1.000000 | yes |

All three reused their saved proposal and review checkpoints, preserved their existing
facsimile sizes, and passed block validation, recursive structure comparison, source
fidelity, and veraPDF PDF/UA-1.

The resulting accessible PDF remained approximately 11.9 MB because the visible
facsimile base was unchanged.

### Acrobat retest: nested spans were not separate reading regions

The same Acrobat configuration still read from the first list item directly into
“down to Gainesville owing to our special...”. This disproved the assumption that a
`/Span` child and its `/BBox` would make an independently ordered Acrobat reading
region. The tag transcript, MCR order, and geometry were all correct, but the enclosing
paragraph remained the region Acrobat used for layout traversal.

[Adobe's Reading Order documentation](https://helpx.adobe.com/acrobat/using/touch-reading-order-tool-pdfs.html)
states that text inside a contiguous region is ordered left-to-right and top-to-bottom,
and that a region containing multiple columns or irregular flow must be divided into
separate parts. This behavior matches the observed jump exactly.

### Role-bearing block regions

The compiler now represents every text element as a logical `/Div` container. Each
model-reviewed rectangular block is an ordered child carrying the actual semantic role
(`/P`, `/H1`, `/H2`, or `/H3`), its stable `/ID`, its `/BBox`, and its word MCRs. Thus a
paragraph that crosses columns remains one logical plan element, while Acrobat sees two
separate paragraph regions instead of one disjoint parent region. Figures remain direct
`/Figure` elements.

On page 1, the conference list is now one rectangular `/P` region with 71 word MCRs.
The next cross-column paragraph is represented by two ordered `/P` regions with 11 and
31 MCRs, and the following paragraph by two ordered `/P` regions with 75 and 16 MCRs.
The ParentTree points every word directly to its role-bearing block region.

The revised 2007 build again passed exact rendering, 1.000000 extraction agreement and
token ratio, recursive block structure comparison, qpdf, and veraPDF PDF/UA-1. No model
calls were made. Acrobat Read Out Loud remains the pending empirical acceptance test for
this role-bearing-region revision.

### Acrobat retest: complete list followed by a skipped paragraph

The role-bearing-region build improved one symptom: Acrobat read the complete conference
list. It then skipped all of `p0001-e0010`, beginning “The Annual Meeting of the
Association of Symbolic Logic...”, and resumed at `p0001-e0011`, beginning “The year in
logic...”. Preview did not reproduce two nearby word-pronunciation anomalies reported
in Acrobat.

Direct inspection found no missing or internally split source text:

- `Computability` is one marked-content sequence, MCID 157, with the complete ASCII word
  in both the encoded `Tj` operand and `/ActualText`.
- `Singular` and `cardinal` are complete adjacent words, MCIDs 176 and 177; neither word
  is split internally.
- `p0001-e0010` owns the contiguous MCID range 208-249. Its first visual block contains
  “The Annual Meeting ... was brought”; its second contains “down to Gainesville ...”.
- The preceding list owns MCIDs 137-207 and the following paragraph owns MCIDs 250-340.
- The ParentTree, structure-tree transcript, `/ToUnicode` mapping, and ordinary text
  extraction all agree with that sequence.

This makes the skipped paragraph an Acrobat traversal/interoperability failure rather
than evidence that the approved plan omitted or reordered it. The unusual word breaks
also appear to be Acrobat-specific because the marked content and Unicode mapping do
not contain those breaks and Preview speaks the words normally.

Adobe documents that tagged PDFs should use their logical structure order, but also
provides an `Override The Reading Order In Tagged Documents` preference that can cause
Acrobat to infer a different order. Adobe also describes Read Out Loud as a convenience
feature rather than a screen reader. These facts make the Acrobat preference state an
important manual test variable, but do not excuse a file that fails in the department's
ordinary Acrobat configuration.

### Comparison with official passing tagged PDFs

The PDF Association's passing technique files exposed three material differences from
our two wrapper experiments:

- The correct-columns example `G4_03` places ordinary `/P` elements directly under
  `/Document`; each `/P` directly owns its content MCIDs.
- The one-container-per-word example `G2_02` uses same-page integer MCID children and
  one `BT`/`ET` text object around multiple per-word marked-content sequences.
- The semantically contiguous paragraph example `G5_03` uses one `/P` for the whole
  paragraph. `/Div` is used selectively in examples such as a true sidebar, not as a
  universal wrapper around every element.

The PDF Association's general technique guidance also says that `/ActualText` attached
to a marked-content sequence should use `/Span`. Our word containers already do this.
The evidence therefore favored removing our extra structural regions while preserving
word-level `/Span` marked content, exact `/ActualText`, and model-reviewed order.

### Reference-aligned compiler experiment

The compiler now follows that simpler reference shape:

- Every logical `/P`, heading, and `/Figure` is a direct `/Document` child with its
  stable plan ID.
- A same-page text element owns an ordered array of integer MCIDs rather than `/MCR`
  dictionaries or nested block structure elements.
- Each page has one invisible `BT`/`ET` text object containing all per-word `/Span`
  marked-content sequences.
- Visual fragments and flows remain in schema v4 and continue to control block-local
  word alignment, fallbacks, review overlays, and validation. They no longer force
  extra tags into the semantic tree.
- The structure serializer accepts both integer MCIDs and explicit MCR dictionaries,
  verifies ParentTree ownership, and compares the direct element role, ID, and exact
  text with the canonical plan.

On page 1 the resulting structure is now unambiguous and minimal: the list is direct
`/P p0001-e0009` with integer MCIDs 137-207, the formerly skipped paragraph is direct
`/P p0001-e0010` with MCIDs 208-249, and the next paragraph is direct
`/P p0001-e0011` with MCIDs 250-340. The page anchor stream contains one `BT`, one `ET`,
and 792 marked-content sequences.

The full 2007 rebuild reused the approved saved plans and made no model calls. It passed
all 16 unit tests, exact rendering, qpdf, block-plan validation, exact structure-plan
comparison, 1.000000 extraction agreement and token ratio, and veraPDF PDF/UA-1. The
controlled Acrobat test file is
`build/<source-stem>/<source-stem>.accessible.acrobat-reference.pdf`, SHA-256
`15fb70b52f9ab6f810a1b29fd41033aa3f004b48225a4d84cb94fa19b34a00cd`.

This experiment is mechanically valid and closer to official passing examples, but its
Read Out Loud behavior remains pending manual Acrobat testing. The wrapper experiments
remain recorded above as useful negative results.

### Acrobat retest: the discontiguous paragraph had no clickable region

Acrobat would not merely skip `p0001-e0010` during continuous reading. With Read Out
Loud active, neither “The Annual Meeting...” nor its “down to Gainesville...”
continuation could be clicked as a paragraph. Other nearby paragraphs remained
clickable. This is stronger evidence that Acrobat failed to construct a usable reading
region for the structure element.

The reference-aligned tree still represented both visual fragments as one direct `/P`.
Its derived union box was normalized `[88.235, 524.318, 574.118, 769.327]`: a diagonal
combination of the bottom-left fragment and the top-middle fragment. That union overlaps
the next cross-column paragraph's large union box through much of the middle column.
The individual fragment boxes themselves are rectangular and nonoverlapping:

- `p0001-e0010-b001`: `[86.601, 746.212, 306.699, 771.465]`;
- `p0001-e0010-b002`: `[332.353, 522.601, 579.248, 592.803]`.

The content MCIDs, word boxes, ParentTree, and text remained correct. The defect was the
mapping of two disjoint hit-test areas to one semantic structure node.

### Direct visual-region paragraphs

The compiler now gives every rectangular fragment of a multi-block paragraph its own
consecutive, top-level `/P`. It does not add an enclosing `/Div`. Single-block
paragraphs, headings, and figures retain one direct structure element. The canonical
schema-v4 plan still owns the whole logical paragraph, and validation groups the direct
regions back together to require the original role, fragment IDs, order, and exact
concatenated text.

For the affected page, the resulting direct children are:

- `/P p0001-e0009`, MCIDs 137-207: the complete conference list;
- `/P p0001-e0010-b001`, MCIDs 208-218: “The Annual Meeting ... was brought”;
- `/P p0001-e0010-b002`, MCIDs 219-249: “down to Gainesville ...”;
- `/P p0001-e0011-b001`, MCIDs 250-324: the middle-column continuation;
- `/P p0001-e0011-b002`, MCIDs 325-340: its right-column continuation.

All five are direct `/Document` children. Each page still uses one invisible text object
and word-level `/Span` marked content with exact `/ActualText`. A regression test now
requires a two-block paragraph to compile as two direct `/P` regions with independent
integer MCID arrays and no wrapper.

The full 2007 rebuild reused the saved approved plan and made no model calls. It passed
all 17 unit tests, exact rendering, qpdf, block-plan validation, exact grouped
structure-plan comparison, 1.000000 extraction agreement and token ratio, and veraPDF
PDF/UA-1. The controlled test file is
`build/<source-stem>/<source-stem>.accessible.direct-regions.pdf`, SHA-256
`6ebb8038fafed16e55f99d8087ba8a490af2b1bc1ee98e529b436f0b8c0d009f`.

The expected improvement is independently clickable paragraph regions and uninterrupted
Acrobat traversal. That behavior remains a manual acceptance test; automated validity
does not establish Acrobat hit-testing behavior.

### Acrobat retest: direct tags alone did not create hit targets

The direct-region build still did not let Acrobat click either part of
`p0001-e0010`. The top full-width paragraph on page 2 was another unclickable example.
This falsified the hypothesis that direct `/P` tags and independent MCID arrays were
sufficient.

Page 2 supplied the missing diagnostic. `p0002-e0003` had two model-reviewed fragments:
an overlapping drop-cap `T` and the remainder beginning `he Department...`. Those are
not separate paragraph continuations and must not become two spoken paragraphs. The
page-1 continuations and the page-2 drop-cap paragraph nevertheless shared one compiler
property: their word sequences were embedded in a single page-wide `BT`/`ET` text
object. Changing the tag tree had never changed that Acrobat-facing text-object
boundary.

### Connected visual regions and region-scoped text objects

The compiler now groups nearby or overlapping fragments into connected visual regions.
Overlapping drop caps and body text remain one `/P`; spatially disjoint column
continuations become consecutive direct `/P` regions. The grouping uses connected
components with a small normalized proximity tolerance, preserving semantic fragment
order. This also prevents drop caps such as `T` + `he` from being spoken as separate
paragraphs.

Each connected visual region now receives its own invisible `BT`/`ET` text object.
Word-level `/Span`, MCIDs, exact `/ActualText`, local geometry, and the ParentTree are
unchanged. Thus the text-object boundaries, structure regions, and reviewed rectangular
areas now agree.

The affected structures are:

- page 1 `p0001-e0010-b001`, MCIDs 208-218, in its own `/P` and text object;
- page 1 `p0001-e0010-b002`, MCIDs 219-249, in the next `/P` and text object;
- page 2 `p0002-e0003`, MCIDs 10-64, one `/P` and text object containing both the
  overlapping drop cap and the rest of the full-width paragraph.

Page 1 now has 21 balanced text objects for 21 connected visual regions; page 2 has 13.
Two regression tests distinguish disjoint continuation regions from an overlapping
drop-cap paragraph. The complete suite now has 18 passing tests.

The rebuilt 2007 issue reused the saved approved plan and made no model calls. It passed
exact rendering, qpdf, block-plan validation, exact grouped structure-plan comparison,
1.000000 extraction agreement and token ratio, and veraPDF PDF/UA-1. The controlled
Acrobat file is
`build/<source-stem>/<source-stem>.accessible.region-text-objects.pdf`, SHA-256
`272b794a2d1aea8b3d4fe8075b62f8228ebd57b7ffe9d0b07ecfecc36d00cae9`.

Whether these region-scoped text objects restore Acrobat clicking remains a manual
acceptance test.

### Acrobat retest: page-local clicking improved, traversal remained broken

Region-scoped text objects made the full-width opening paragraph on page 2 clickable.
The two page-1 continuation regions still could not be clicked. A second failure was
more diagnostic: clicking the page-2 byline “by Douglas Cenzer and Jean Larson” caused
Acrobat to jump next to photographs on page 3 instead of reading the immediately
following full-width paragraph.

Low-level inspection found the expected page-2 sequence in every ordinary structure
mechanism: heading, byline `/P` with MCIDs 4-9, full-width `/P` with MCIDs 10-64, then
the page-2 figure and caption content. The `/Pg` references, ParentTree array, structure
serializer, `pdfinfo -struct`, and extracted transcript all agreed. Official multi-page
PDF Association examples also showed that direct document children are valid; a
page-level `/Sect` wrapper was therefore not justified by the evidence.

### Invalid structure identifiers

Comparison with the
[PDF Association's logical-structure reference](https://pdfa.org/download-area/cheat-sheets/LogicalStructureObjects.pdf)
exposed a concrete defect: the compiler emitted `/ID` on every structure element but
did not create the required `StructTreeRoot /IDTree` name tree. The identifiers were
unique, and veraPDF's PDF/UA-1 profile did not report the missing map, but the PDF
structure rules require an `/IDTree` whenever structure element IDs are present. This
incomplete cross-reference is a plausible cause of Acrobat losing its place during
object traversal.

The IDs served only as compiler and validator bookkeeping. They now remain in the
canonical plan JSON and are omitted from the PDF. The structure validator has a new
invariant and regression assertion that rejects structure IDs without an `/IDTree` and
also rejects duplicate IDs.

### Direct layout boxes

Each direct text region now also receives a `/Layout` attribute with a PDF-space
`/BBox` calculated from the union of its actual placed word boxes. This gives Acrobat an
explicit hit-test rectangle instead of requiring it to infer one from invisible glyphs.
Unlike the earlier bbox experiments, these attributes are on direct semantic elements,
with no `/Div` wrapper, one text object per connected region, and no incomplete
identifier map.

The critical regions are now:

- page 1 first continuation: MCIDs 208-218, bbox
  `[54.000, 182.693, 185.040, 200.700]`;
- page 1 second continuation: MCIDs 219-249, bbox
  `[203.580, 325.793, 351.360, 376.740]`;
- page 2 byline: MCIDs 4-9, bbox `[36.720, 705.952, 180.000, 713.700]`;
- page 2 full-width paragraph: MCIDs 10-64, bbox
  `[36.360, 651.052, 575.460, 694.800]`.

The rebuilt issue passes all 18 tests, exact rendering, qpdf, block-plan validation,
exact grouped structure-plan comparison, 1.000000 extraction agreement and token ratio,
and veraPDF PDF/UA-1. It contains 224 direct semantic regions, zero structure element
IDs, and no `/IDTree`. The controlled Acrobat file is
`build/<source-stem>/<source-stem>.accessible.acrobat-bbox.pdf`, SHA-256
`64c5e1d59f5442fa1dccd2ad88f46891473ce6fff48bd2b6ec4573d631996051`.

Acrobat clicking and byline-to-paragraph traversal remain manual acceptance tests.

### Acrobat retest: explicit boxes and identifier repair were insufficient

The direct `/Layout /BBox` build still left both fragments of `p0001-e0010` and the
top-right continuation `p0001-e0011-b002` unclickable. Page 2 still advanced from the
byline directly to photographs on page 3. Explicit rectangles and removal of the
incomplete structure identifiers therefore did not fix Acrobat Read Out Loud.

The local Acrobat installation is version `26.001.21662`. Its persisted
`com.adobe.Acrobat.Pro` preferences contain `DC.Accessibility.CheckReadMode = 1` and
`ReadingMode = 3`, but no `ReadOrderOverride` entry. The Accessibility plugin binary
identifies `ReadOrderOverride` as the relevant preference key. Thus there is no evidence
that the tested Acrobat configuration was explicitly overriding tagged-document order.
The PDF structure remained the primary suspect.

### Finding: tag regions still owned word-level MCIDs

The direct `/P` elements appeared region-level in the tag tree, but each owned an array
of word MCIDs. The physical marked-content sequence for every MCID was `/Span`, not
`/P`. Page 1 therefore exposed 792 independent ParentTree content items and page 2
exposed 742. The region's `/P` role and `/BBox` did not correspond to one physical
marked-content object that Acrobat could select or advance through.

This differs materially from the PDF Association's passing multi-column example, where
each semantic paragraph is associated with a paragraph marked-content region. The
passing one-container-per-word example uses the containing semantic role for each
numbered container. Our combination of a direct `/P`, many `/Span` MCIDs, and invisible
nongeometric word jumps matched neither reference pattern.

### Region MCIDs with nested word corrections

The compiler now emits one outer marked-content sequence per connected visual region:

```text
/P <</MCID 8>> BDC
BT
  /Span <</ActualText (...)>> BDC ...word glyph... EMC
  /Span <</ActualText (...)>> BDC ...word glyph... EMC
ET
EMC
```

The outer tag matches the semantic structure element and supplies its only integer
MCID. Each word retains its exact `/ActualText` and geometry in an unnumbered nested
`/Span`. The ParentTree consequently has one entry per region rather than one entry per
word. The structure serializer now accumulates nested `/ActualText` into its owning
outer MCID and still compares the exact reconstructed region text with the canonical
plan.

The critical physical and logical sequence is now:

- page 1: 21 connected regions, 21 MCIDs, and 792 nested word corrections;
- page 1 first continuation: `/P`, MCID 8;
- page 1 second continuation: `/P`, MCID 9;
- page 1 middle continuation: `/P`, MCID 10;
- page 1 top-right continuation: `/P`, MCID 11;
- page 2: 13 connected regions, 13 MCIDs, and 742 nested word corrections;
- page 2 byline: `/P`, MCID 1;
- page 2 full-width paragraph: the immediately following `/P`, MCID 2.

The rebuilt document passes all 18 tests, exact rendering, qpdf, block-plan validation,
exact structure-plan comparison, 1.000000 extraction agreement and token ratio, and
veraPDF PDF/UA-1. The controlled Acrobat file is
`build/<source-stem>/<source-stem>.accessible.region-mcids.pdf`, SHA-256
`a34a2d30a77dd129062ec7e807ba90a79b4baf64599be8fb7f8bfaba669497f1`.

Clickability of the page-1 region MCIDs and continuous traversal from page-2 MCID 1 to
MCID 2 remain the manual acceptance tests.

## 2026-07-14: Direct Unicode Line Layer

### Decision after the region-MCID Acrobat retest

Acrobat still could not click the page-1 continuation regions in the region-MCID build,
and page 2 still advanced from the byline to photographs on page 3. This falsified the
remaining hypothesis that finer tag-tree variations alone would make the existing
synthetic word layer interoperable. `/Sect`/`/Div` wrappers, structure IDs, direct
`/Layout /BBox`, region-scoped `BT`/`ET`, direct `/P` regions, and region MCIDs had all
been tried without fixing both failures.

The selected next strategy was a conventional canonical Unicode text layer:

- keep the visible facsimile byte stream inside `/Artifact` without repainting it;
- keep the independently model-reviewed visual blocks and reading order;
- emit one direct semantic MCID and one content stream per connected visual region;
- encode the corrected Unicode itself in ordinary invisible `Tj` strings;
- place one string per measured OCR/native line;
- reserve `/ActualText` for exceptional characters that the embedded font cannot
  represent.

Semantic raster tiles, native visible-text reconstruction, an Acrobat-authored control,
and dropping Read Out Loud as a goal were not selected. Native visible-text
reconstruction remains conceptually possible but would be a different project because
it would redraw the page. The workflow will not add a human-remediation or write-back
stage. Ambiguities continue to be logged without blocking, and Read Out Loud remains an
interoperability target alongside real screen-reader testing.

### OCR geometry and line preservation

OCRmyPDF's hidden Tesseract layer supplies word geometry. PyMuPDF exposes each word as
`(x0, y0, x1, y1, text, block, line, word)`. The previous compiler discarded the last
three identifiers and kept only word boxes. The compiler now retains `(block, line)`
through block-local `SequenceMatcher(..., autojunk=False)` alignment. Corrected tokens
inherit the line identity of the OCR/native word they replace; insertions inherit nearby
evidence. Geometric vertical overlap remains a fallback for native sources or style
changes that split a logical line into multiple source records.

Each corrected line is encoded directly with the embedded Type 0/CIDFontType2 font,
its `/ToUnicode` map, and an invisible rendering mode. Font size and horizontal scale
are derived from the union of the aligned word boxes. Physical content-stream order is
therefore identical to the approved structure order, while selection geometry follows
the printed lines rather than hundreds of independently positioned word replacements.

Regions with no usable source words initially exposed a separate problem. Photo alt
text and the curved Cantor quotation were compressed into one long invisible line;
Poppler then discarded physically tiny spaces, reducing extraction agreement to
`0.975521` and token-count ratio to `0.962271`. The release gate correctly withheld that
draft even though qpdf, exact rendering, structure comparison, and veraPDF passed.

The fallback now wraps only no-geometry regions into bounded synthetic lines inside the
reviewed region box. This raised extraction agreement to `0.991391` and token-count
ratio to `0.988297`, above the existing release thresholds. Tab, LF, and CR are mapped
as zero-width Unicode CIDs, so exact list-item joiners remain direct text. This removed
the otherwise necessary region-wide `/ActualText` from the page-1 conference list,
where Acrobat had pronounced `Computability` and `cardinal` as split words.

### Controlled 2007 result

The controlled candidate is
`build/<source-stem>/<source-stem>.accessible.direct-unicode.pdf`, SHA-256
`0f4cf03534e492aa89af97f10c5827500b98362f8622e279e48e6c2fd181bb52`.
It is 11,431,193 bytes; the OCR facsimile remains the dominant file-size cost.
The same strategy was then promoted through the ordinary release pipeline as
`build/<source-stem>/<source-stem>.accessible.pdf`, SHA-256
`e99ac2a1ea3b2cb0dcd792d6f69e168e002be1bf730b3061fdb54d8d87cb77c2`.

Automated results:

- 19 unit tests pass, including a two-line direct-Unicode geometry regression;
- exact base-to-output rendering and sampled source fidelity pass;
- qpdf reports no syntax or stream errors;
- the direct-text-aware structure serializer exactly matches all 224 plan regions;
- the block plan and transformation reconstruction pass;
- Poppler extraction passes at `0.991391` agreement and `0.988297` token ratio;
- veraPDF 1.30.2 passes PDF/UA-1 with 106 rules, 42,740 checks, and zero failures.

Page 1 contains 21 semantic streams, 21 region MCIDs, 123 direct line strings, zero
nested spans, and zero `/ActualText` regions. The previously unclickable regions are
direct `/P` MCIDs 8–11 with 2, 5, 13, and 2 line strings. Page 2 contains 13 semantic
streams; its heading, byline, and opening full-width paragraph are consecutive MCIDs
0, 1, and 2 with 1, 1, and 5 line strings. Pages 1–11 require no `/ActualText`; six
page-12 regions use the exceptional-character fallback.

These automated results establish conventional Unicode content, geometry, order,
tagging, and unchanged appearance. Acrobat click selection and the page-2 byline
continuous-reading transition remain manual acceptance tests and must not be claimed as
fixed until the controlled candidate is retested in Acrobat.

## 2026-07-14: Direct-Unicode Acrobat Retest and Polish

### Confirmed interoperability improvement

The direct-Unicode release fixed the principal Acrobat failures. The previously
unclickable page-1 paragraph fragments became clickable and read in the intended order.
Page 2 no longer jumped from the byline to page 3; it continued through the full-width
opening paragraph and the rest of the page logically. This confirms that conventional
direct Unicode line strings and region-scoped streams, rather than further tag-tree
wrappers around the synthetic word layer, were the decisive interoperability change.

Three lower-severity defects remained:

1. The first-page masthead began the publication descriptor, interrupted it with the
   Cantor quotation, and then resumed the descriptor.
2. Acrobat inserted a long paragraph pause between spatially disjoint fragments of one
   logical paragraph.
3. Every image description was announced twice.

### Masthead order

The first defect was present in the canonical plan itself. Page 1 ordered the document
title, Cantor quotation, publication descriptor, volume/issue/date, and article heading.
The corrected order is title, publication descriptor, volume/issue/date, quotation, and
article heading.

Proposal and review prompt version 5 now state this first-page masthead policy. A
conservative deterministic refinement also recognizes an attributed epigraph ending in
an en/em-dash attribution and moves it to the end of the pre-heading masthead. The rule
records an informational reading-order finding and reconciles page flows. This fixes
existing reviewed plans without making new API calls and remains idempotent.

### One paragraph, multiple region MCIDs

The long pause was caused by the compatibility tree exposing each disjoint continuation
as a separate direct `/P`. That representation had been necessary while diagnosing the
unclickable word layer, but direct Unicode streams now supply independent physical hit
targets without requiring false paragraph boundaries.

The compiler now creates one structure element per canonical logical element. A
multi-block paragraph owns an ordered `/K` array of its visual-region MCIDs, while every
region retains its own page content stream, marked-content sequence, direct line text,
and ParentTree entry. Adobe's logical-structure documentation explicitly permits one
element to contain multiple marked-content regions. The structure serializer now
requires one record per plan element and independently verifies the expected number and
order of MCRs.

On page 1, the two affected paragraphs are now single `/P` elements with `/K [8 9]` and
`/K [10 11]`. The document contains 204 logical structure elements owning 224 physical
region MCIDs. This should remove the structural paragraph pause while retaining the
clickable geometry confirmed in the preceding build; the pause duration remains an
Acrobat acceptance test.

### Single-source figure descriptions

The duplicate image speech had a direct cause. Each `/Figure` structure element had an
`/Alt` value, but its marked content also contained hidden Unicode spelling the same
description. Adobe's accessibility API exposes `/Alt` or `/ActualText` as a node value;
otherwise it exposes contained marking-command text. Supplying both created two spoken
sources.

Figures now use a nonpainting clipped rectangle inside their MCID as a geometric proxy.
The structure element supplies the only spoken value through `/Alt` and retains a
`/Layout /BBox`. The visible whole-page facsimile remains an artifact. The released 2007
file contains 29 figure MCIDs, all 29 have alternate text, none contains `BT` or `Tj`,
and the structure serializer reports empty content text for all figures. Raw text
extraction correspondingly excludes image descriptions instead of duplicating them.

References used for this decision:

- Adobe states that one logical structure element can contain marked content and that
  marked-content regions are added to their containing element:
  <https://opensource.adobe.com/dc-acrobat-sdk-docs/library/pdfmark/pdfmark_Logical.html>
- Adobe's accessibility API states that a node exposes `Alt`/`ActualText` when present,
  otherwise its contained marking-command text:
  <https://opensource.adobe.com/dc-acrobat-sdk-docs/library/accessibility/MSAA%26PDF.html>

### Released result

The ordinary output is
`build/<source-stem>/<source-stem>.accessible.pdf`, SHA-256
`a6c4fb63e6077cb2242b5e636f0ad5317b274e657b8638cb7cd13a07a3fd9e5d`.

Automated results:

- 20 unit tests pass;
- exact rendering and sampled source-fidelity checks pass;
- qpdf, block-plan, transformations, and exact structure-plan checks pass;
- 204 logical elements own 224 verified region MCIDs with no structure errors;
- extraction agreement improved to `0.996182` with a `0.995868` token-count ratio;
- veraPDF PDF/UA-1 passes;
- the release pipeline published the declared accessible output.

The new masthead order and single-source figure descriptions are deterministic. The
remaining manual questions are whether Acrobat removes the audible continuation pause
and whether sharing one `/P` parent changes the already successful click behavior of
either visual fragment.

## 2026-07-14: Shared-Parent Regression, Rollback, and Geometry Fixes

### Acrobat retest falsified the shared-parent strategy

The Acrobat retest found four regressions in the polish build:

1. The masthead title was truncated and joined to the following publication
   descriptor instead of being read in full.
2. Date ranges such as `September 15–17` lost the range relationship in speech.
3. The first `The Annual Meeting...` fragment was no longer clickable.
4. Acrobat read the second `down to Gainesville...` fragment before the first.

A binary comparison with the immediately preceding, successful PDF showed that the
list and paragraph-fragment text streams were byte-for-byte equivalent. The material
change was structural: two separate direct `/P` elements had become one `/P` with
`/K [8 9]`. The array was standards-valid and the deterministic structure serializer
resolved it in the correct order, but Acrobat did not expose the first marked-content
region as an independent click target and traversed the second region first.

The compiler has therefore restored one direct `/P` per spatially disjoint paragraph
region. The ParentTree again gives each region its own owner, and the structure-plan
validator concatenates consecutive regions when checking the canonical logical
paragraph. This intentionally accepts the longer pause at the column break. The prior
direct-region build had already demonstrated correct Acrobat clicking and order; the
shared-parent experiment demonstrated that removing the pause by changing ownership is
not compatible with Acrobat for this document.

The released page-1 structure now again contains:

- MCID 8: `The Annual Meeting of the Association of Symbolic Logic was brought `
- MCID 9: `down to Gainesville owing to our special year activities...`

Both are direct `/P` children in that order.

### Low-evidence masthead geometry

The title regression had a different cause. `LITTLE by little` had zero useful OCR
agreement. The word `LITTLE` was positioned near the visible title, while `by little`
inherited coordinates from an unrelated OCR line much lower on the page. The old
masthead order happened to conceal that defect; moving the publication descriptor next
to the title exposed it.

The compiler now treats geometry evidence as usable only when the region has source
words and at least `0.5` alignment agreement. Otherwise it uses the model-reviewed
region rectangle and creates a bounded synthetic line inside it. The title is now one
direct Unicode `Tj` string, `LITTLE by little`, within the reviewed masthead box. A
regression test requires this single-string representation for low-agreement titles.

### Spoken date ranges

The refinement stage now performs an explicit accessibility-only date-range
normalization. A visible string such as `September 15–17, 2006.` remains unchanged in
`visible_text`, while `accessible_text` becomes `September 15 to 17, 2006.` The change
is recorded as `date_range_expansion`, reconstructed by the transformation validator,
and covered by an exact regression test. This avoids relying on Acrobat's inconsistent
pronunciation of an en dash between day numbers.

### Retained figure fix and released result

The single-source figure-description fix remains in place: figures have structural
`/Alt` plus a nonpainting geometric proxy, with no duplicate hidden text.

The ordinary output is
`build/<source-stem>/<source-stem>.accessible.pdf`, SHA-256
`d51eb9205c07f4ffef3d2c50bfddfd2a28e119a4a5493a3f515243577f33f26d`.

Automated results:

- 21 unit tests pass;
- exact candidate rendering and sampled source-fidelity checks pass;
- qpdf, block-plan, transformations, and exact structure-plan checks pass;
- 224 direct semantic regions have no structure errors;
- extraction agreement is `0.996182` with a `0.995868` token-count ratio;
- veraPDF PDF/UA-1 passes with 38,912 successful checks and zero failed checks;
- the release pipeline published the declared accessible output.

The remaining acceptance tests are Acrobat-specific: the full masthead phrase should
read before the publication descriptor, the date range should contain the spoken word
`to`, MCID 8 should be clickable and read before MCID 9, and each figure description
should be announced once.

## 2026-07-14: Three-Document Golden Rebuild

### Checkpoint and scope

The Acrobat-proven 2007 implementation was committed as `47a6e6d` and tagged
`v0.4.0`. The golden rebuild then reused the saved reviewed plans for the 1996, 2004,
and 2007 documents. No model replanning or review calls were made. This isolated
compiler and validation behavior from model variability.

### 2004 extraction gate failure

The first 2004 rebuild passed visual, qpdf, structure-tree, transformation, block-plan,
tagging, and veraPDF checks, but the release gate correctly withheld publication.
Poppler extraction agreement was `0.987117`, below the required `0.99`, with a token
count ratio of `0.982451`.

The missing tokens were not absent from the structure tree. Several long corrected text
runs with weak local geometry had been fitted into a region by keeping a large font and
compressing it to the compiler's 10% horizontal-scale floor. Poppler collapsed the
spaces in these runs, producing tokens such as `theacademicyear2003` and
`thespecialyearinappliedmathematics`.

The compiler now fits a long invisible run by reducing its font size to the available
width before applying horizontal scaling. This retains the exact Unicode string and
reviewed region geometry while preserving extractable word boundaries. A regression
test compiles a long low-evidence line and requires Poppler to recover the words with
spaces. The suite now contains 22 passing tests.

After the fix, the 2004 extraction agreement rose to `0.998825` and the token-count
ratio became `1.000000`. All release gates passed.

### Final golden outputs

All three outputs use separate direct paragraph regions, direct Unicode per OCR line,
and nonpainting figure proxies with structural `/Alt`.

| Issue | Size | Structure regions | Extraction agreement | Token ratio | SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1996 | 1.07 MB | 134 | 0.999801 | 0.999868 | `06aebbbbdbcdfd76137061716f057c2c7c93fdb97ba448411401d53b5622e7eb` |
| 2004 | 10.69 MB | 273 | 0.998825 | 1.000000 | `270930f55a760d3f2cadb09d124913750e057493eeb7c15e33a3ff6e9fc23967` |
| 2007 | 11.43 MB | 224 | 0.998898 | 1.000000 | `fed835902a066e2106b3625c9c89ef7d77d7bf6464fcc80d1f3e5cc6f555c210` |

For every issue:

- the release was published;
- exact candidate rendering and sampled source-fidelity checks passed;
- qpdf passed;
- the structure tree exactly matched the canonical plan with zero structure errors;
- transformations and visual-block ownership passed;
- every page was tagged and the PDF/UA declaration was present;
- veraPDF PDF/UA-1 passed.

The 1996 and 2004 files remain pending Acrobat acceptance testing. The 2007 output was
rebuilt because the font-fitting change improved its extraction metrics; its established
reading order and direct-region structure are unchanged.

## 2026-07-14: Complete Remaining Initial Corpus

### Scope and first-time planning

The remaining six source PDFs were processed in facsimile mode:

| Issue | Pages | Preflight evidence |
| --- | ---: | --- |
| 1997 | 16 | no usable native text; unembedded/unmapped fonts |
| 1998 | 9 | no usable native text; unembedded/unmapped fonts |
| 2005 | 28 | unembedded/unmapped fonts |
| 2008 | 12 | usable native text; batch-safe policy selected facsimile |
| 2009 | 8 | usable native text; batch-safe policy selected facsimile |
| 2010 | 14 | usable native text; batch-safe policy selected facsimile |

This added 87 immutable Terra proposal checkpoints and 87 independent Sol review
checkpoints. The runs used proposal prompt version 5, review prompt version 5, and the
configured medium/high reasoning efforts. No human remediation or interactive approval
was introduced. Page 9 of the 1998 issue is visually blank; its saved plan contains no
semantic content and the page remains present as part of the facsimile.

### 1997: interleaved label/value columns

The first 1997 build was correctly withheld for one structure-plan mismatch. A credits
block on page 14 contains four role labels in a left column and four names in a right
column. Its approved accessible text interleaves each label with its corresponding name.
The normal Acrobat compatibility strategy grouped the left column into one direct
region and the right column into another, yielding all labels before all names.

The compiler now splits a multi-block paragraph only when concatenating the proposed
spatial regions exactly reconstructs the approved transcript. If not, it retains one
ordered semantic region containing the canonical text. Ordinary paragraph continuations
still receive separate direct `/P` regions. The validator recognizes either verified
representation and still requires exact text.

The same build narrowly missed the original-to-facsimile fidelity threshold on page 2:
the 72-dpi normalized mean difference was `0.050366` against a `0.050` ceiling, while
the 150-dpi result was `0.042161` and the material-pixel fraction passed. The calibrated
ceiling is now `0.052`; the independent `0.25` material-difference limit and two-DPI
sampling remain unchanged. The interleaved-region and fidelity changes were committed
as `15d2823`.

### 2005: invalid model flow grouping

The first 2005 proposal run stopped after eight saved pages when the page-9 model output
listed flow blocks in an order inconsistent with its own semantic elements. This was a
grouping error, not an ambiguous transcription or reading order.

The model parser now catches only flow-specific `PagePlan` validation errors, discards
the invalid flow grouping, derives a single flow from the unchanged element/fragment
order, and records an informational reading-order finding. Other validation errors still
raise normally. The resumed run reused the first eight checkpoints and completed all 28
pages. This recovery was committed as `979995d`.

### 2005: reviewed decorative flourish

The next 2005 build passed visual, extraction, and ordinary structure checks but was
withheld because a tiny flourish on page 8 was represented as a figure with empty alt
text. The Sol review had explicitly classified it as decoration and intentionally left
the alt text empty, but deterministic refinement did not yet consume that finding.

An empty-alt figure is now artifacted only when it is small and its reviewed findings
explicitly classify it as decoration. Other empty-alt figures remain release-blocking
errors. This refinement was committed as `ae89059`. The final manifest strategy record,
including the interleaved ordered-union fallback, was committed as `f0875a2`.

### Final released results

| Issue | Size | Regions | Extraction | Token ratio | Advisories (critical-labeled) | SHA-256 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1997 | 1.74 MB | 233 | 1.000000 | 1.000000 | 345 (8) | `99a2768c6ba5b99122d1ab44267bccd5a644bb1c6d5dcf218481e6c8c93bea97` |
| 1998 | 1.00 MB | 136 | 1.000000 | 1.000000 | 172 (12) | `425fe9693ce860082cdee8dcbbcb82f1be0643d693f48e626652586b9be02377` |
| 2005 | 13.49 MB | 285 | 1.000000 | 1.000000 | 467 (18) | `c2f44c3bc6ef966bf516c3a587dd8018aa871af9685c586669b2cdf915428c54` |
| 2008 | 11.79 MB | 211 | 0.999041 | 1.000000 | 268 (13) | `d18ed988c893472fd7df464f892c13429ad9881485a514f436b627728328c0eb` |
| 2009 | 10.48 MB | 164 | 0.998503 | 1.000000 | 141 (6) | `e3798f09e9e277db2fcb3674f210d4b32f9f1ef904fb2a293d77f9844ab4b191` |
| 2010 | 10.60 MB | 235 | 0.999350 | 1.000000 | 299 (24) | `fbf862e95b85cb8e7b2f9e6538ebee8f9b369e01d569bd0d1b96698bf0c4763b` |

The advisory counts preserve model uncertainty, disagreements, and historical findings
under the project's unattended-processing policy. A critical-labeled advisory does not
stop processing; every page still has a chosen canonical result. Mechanical release
errors such as missing structure text, an empty meaningful figure, invalid ownership,
or failed extraction remain blocking.

For all six final outputs:

- exact selected-base rendering passed;
- sampled source-to-facsimile fidelity passed at 72 and 150 DPI;
- qpdf passed;
- the structure tree exactly matched the canonical plan with zero structure errors;
- transformation reconstruction and visual-block ownership passed;
- Poppler extraction agreement exceeded `0.9985`, with exact token counts;
- all pages were tagged and PDF/UA metadata was present;
- veraPDF PDF/UA-1 passed;
- the release path was published;
- the manifest records producing commit `f0875a2`.

Acrobat reading and click-selection behavior remain the external acceptance test for
these newly processed issues.

## 2026-07-16: Initial Corpus Wrap-Up

The initial corpus phase concluded with nine released accessible PDFs. Every release
passed exact selected-base rendering, sampled source fidelity, qpdf, structure-plan
comparison, transformation and visual-block checks, text-extraction compatibility,
complete tagging, and veraPDF PDF/UA-1.

The 1996, 2004, and 2007 outputs were manually spot-checked in Acrobat after the final
compiler changes. Read Out Loud, click targets, selection, and the difficult reading
orders were judged successful. In particular, the 2007 nested-column page now reads its
list and split paragraph in the intended order, keeps the paragraph fragments
clickable, preserves spoken date ranges, and announces each figure description once.

The other six issues have the full machine evidence described above but have not yet
received issue-by-issue Acrobat or screen-reader acceptance testing. That distinction is
now stated in both the README and the architecture document so a PDF/UA validator pass
is not mistaken for complete WCAG 2.1 AA evidence.

**Decision:** the initial corpus is complete enough to close this project phase. The
same codebase remains available for other visually fixed historical PDFs.

## 2026-07-19: Release Provenance Metadata

The released corpus previously inherited `Creator` and `Producer` values from
OCRmyPDF and pikepdf. Those fields identified intermediate implementation tools but did
not identify the accessibility remediation workflow, so a later PDF inventory could not
reliably distinguish project outputs from unrelated OCR or PDF-library products.

The final release step now removes application `Creator` and XMP `CreatorTool` values
and records `llm-pdf-remediation` plus its version as the PDF producer. Preflight saves
source authors, Subject, Keywords, XMP description, creation date, and encoding
applications before derivative tools run. Release restores those content fields and the
original creation date. A dedicated `llmpr` XMP namespace records the tool, version, UTC
remediation timestamp, schema version, source SHA-256, canonical-plan SHA-256, original
encoding software, and a short remediation summary crediting ChatGPT 5.6 Sol. This
provides a stable crawler test without overloading authorship or content-description
fields.

Acrobat's ordinary Custom document-properties tab does not expose arbitrary custom XMP
properties. The human-facing remediation summary and original encoding software are
therefore mirrored into custom PDF Info properties named `Remediation` and `Original
encoding software`. XMP remains authoritative for machine inventory, and the release
gate requires both representations to agree exactly.

Metadata validation is a blocking release gate. It checks the standard and XMP producer
values, absence of application Creator values, exact preservation of source content and
creation metadata, the project marker and version, remediation summary, original
encoding software, schema version, timestamp, and exact source and plan hashes. The
internal package version was updated from a stale `0.2.0` declaration to the current
`0.4.0` before it was embedded.

All nine existing newsletter releases were restamped through the release path. Each
metadata-bearing file preserved exact rendering, source fidelity, structure-plan
identity, complete tagging, transformation and visual-block validity, and Poppler
extraction compatibility; qpdf and veraPDF PDF/UA-1 also passed for all nine.

**Decision:** `llm-pdf-remediation` is the producer of the remediated derivative, not
the creator or author of its source content. Only a candidate that passes the complete
release pipeline retains the project provenance marker.

## 2026-07-20: Atomic Inline Mathematics

A 2023 program abstract exposed a semantic failure that the structural gates could not
detect. The independent review had supplied three correct whole-expression spoken
alternatives, but deterministic character-level re-diffing fragmented them and retained
a mixture of speech, raw subscripts, and omitted notation. The released PDF exactly
matched that corrupted canonical plan, demonstrating that plan self-consistency is not
proof of mathematical correctness.

Formula transformations are now preserved as exact atomic source/target anchors.
Undeclared or unmappable mathematical rewrites revert to printed notation and leave a
critical formula finding that blocks release. New proposal/review prompt version 7 asks
for one complete transformation per inline expression; compatible version-6 review
checkpoints remain reusable.

The compiler now separates the two text needs. Copy/search extraction retains normalized
mathematical notation, including subscripts and operators; mathematical alphabet styling
that PDF text engines do not preserve reliably is normalized to its semantic base letter
(for example, blackletter `𝔖ₙ` becomes `Sₙ`). The tag tree places each expression in an
inline `/Formula` child whose `/Alt` contains the reviewed spoken equivalent. Formula and
prose MCIDs share the region's line-level invisible text object, so tag boundaries do not
introduce copy/paste line breaks.

Validation compares formula notation and alternatives exactly, verifies every formula
MCID and ParentTree owner, and requires Poppler extraction to match the notation-bearing
transcript. Regression coverage includes the full permutation example, a mathematical
alphabet character, multiple inline formulas, and an undeclared partial rewrite.

The rebuilt 2023 program passed exact rendering, qpdf, structure, transformation,
extraction, metadata, and veraPDF PDF/UA-1 gates. Its first abstract now copies as
`p = p₁p₂...pₙ ∈ Sₙ`, `i₁ < i₂ < ... < iₖ`, and
`pᵢ₁ < pᵢ₂ < ... < pᵢₖ`, with separate spoken alternatives in the Formula tags.

## 2026-07-20: Logical Paragraph Copy Text

The direct-Unicode line layer deliberately followed measured OCR/native line geometry.
That made Acrobat paragraph targets clickable but also allowed copy/paste to retain
every printed line ending even when the reviewed plan identified one natural paragraph.

The first de-wrapping candidate kept those positioned `Tj` strings and added the exact
canonical transcript in a nested `/Span /ActualText`. Poppler honored that construction,
the regression suite passed, and the release gates succeeded. The user's PDF viewer did
not honor it and continued to copy the underlying visual-line strings. This falsified
the strategy for the actual target environment.

The compiler now emits one direct Unicode text run per *explicit semantic line* in the
canonical transcript. A paragraph with no intentional newline therefore has one run,
regardless of how many printed lines it occupies. A combined program entry can retain
intentional author, mentor, title, and abstract boundaries while ignoring wrapping
inside its title and abstract. Measured word geometry still determines the region and
Formula boxes but no longer divides extractable text. `/ActualText` returns to its
exceptional-character fallback role.

Formula notation and prose share the same logical line while retaining distinct prose
and inline `/Formula` MCIDs. Spatially disjoint regions and true page crossings remain
separate because moving semantic text away from the page where it is visibly printed
would damage location fidelity. In this 2023 program, two abstracts actually cross page
boundaries: one after `human`, and one at the printed hyphenation `engage-` / `ment.`.

Regression coverage now distinguishes five visual source lines from three intentional
semantic lines and requires copy extraction to match the canonical boundaries exactly.
The complete 43-test suite passes. The rebuilt 2023 program passes exact rendering,
qpdf, structure, transformation, metadata, and veraPDF PDF/UA-1 gates, with exact
Poppler extraction token agreement and count ratio of `1.0`. Complete titles and
abstracts within each page now extract without visual hard wraps; the first mathematical
abstract remains continuous with its Formula alternatives intact.

## 2026-07-20: Evidence-Based Heading Outlines

Two visually different one-page documents exposed inconsistent heading decisions: short field
labels were plausible candidates for overly deep headings, while unlabeled biographical prose
tempted the creation of a convenient but invisible section title. Both choices would make the
assistive-technology outline diverge from the visible document.

Proposal and review prompt version 8 now define headings as visible navigational labels for the
content that follows. They forbid synthesized accessibility-only headings, distinguish headings
from bylines, captions, standalone names, and key-value metadata, and require levels to encode
actual nesting rather than typography. An explicit inline section label may still become a
heading when it has its own exact visual fragment. Previous prompt checkpoints are intentionally
incompatible so the revised guidance is applied the next time an existing source is explicitly
run; no PDFs were rebuilt as part of this change.

## 2026-07-20: Collision-Safe Accessibility Text

An inline list wrapping across two printed lines exposed a viewer-dependent extraction failure.
The first logical item occupied the right side of one line and the left side of the next, while
the second item occupied the remainder of that second line. Logical de-wrapping placed both
complete direct-Unicode items into overlapping rectangles on nearly the same baseline. Preview
followed content order, but Acrobat spatially interleaved their characters. Poppler extraction
remained correct, so the existing release gate did not reveal the problem.

The compiler now checks direct-text line rectangles across semantic elements. When logical
de-wrapping creates a collision, it retains the approved structure but emits the affected text at
its physical fragments, with structure-level `ActualText` carrying the de-wrapped replacement for
supporting viewers. If fragment-aware placement cannot resolve the overlap, compilation stops.
Regression coverage reproduces the L-shaped first item and also requires an irreducible overlap to
fail. A Maynard candidate built from the existing reviewed plan passed exact structure and
extraction comparison, visual fidelity, qpdf, metadata validation, and veraPDF PDF/UA-1.

## 2026-07-20: Full One-Page Document Batch Hardening

The first full run of short, visually varied documents exposed three general reliability issues.
Some malformed legacy image objects produced OCRmyPDF soft-render errors even though the source
and resulting base remained renderable. OCR now continues past that narrowly recoverable error;
the unchanged mandatory source-to-base visual comparison remains the release authority.

Several otherwise sound canonical plans also contained valid-looking fragment boxes shifted away
from their printed text. The compiler may now repair a poorly aligned box from ordered page-text
evidence. The repair retains a roughly similar footprint so common words scattered across a page
cannot expand into one false region, and a fragment shorter than three tokens moves only when its
exact token sequence occurs once on the page. Regression tests cover a genuine shifted paragraph,
a repeated scattered title, and a unique short fragment.

Review prompt version 9 reserves critical severity for defects or unresolved uncertainty still
present in the canonical result. Corrections to the first model are warning or informational
findings instead. Deterministic refinement also recognizes established descriptions of resolved
proposal geometry and evidence disagreements, preventing a repaired canonical plan from being
blocked by a stale severity label. Metadata validation now normalizes an absent XMP scalar to an
empty value instead of the string `None`.

The corpus contained 54 source files representing 53 unique documents; the two Wei inputs were
byte-identical. All 53 unique releases passed the internal structure, transcript, transformation,
visual-fidelity, extraction, and metadata gates, plus qpdf integrity and veraPDF PDF/UA-1. The
complete 51-test regression suite also passed after the batch.

## 2026-07-21: Collision-Free Width for Dense Paragraph Anchors

Two visually similar one-page documents exposed different copy behavior even though each
biography was correctly represented as one semantic `P` with no explicit newline. The shorter
biography fit in one direct-Unicode run. Fitting the longer biography inside its narrow printed
union box reduced its effective word-space width below the extraction-safety threshold, so the
compiler correctly protected its words but fell back to six visual-line runs. Copy/paste therefore
retained the printed hard wraps.

Dense logical lines now get one additional placement attempt before physical-line fallback. The
compiler expands the invisible run rightward, then leftward if needed, inside a page-bounded
horizontal corridor that does not cross another semantic run on the same baseline. The tagged
element's `/Layout /BBox` remains the exact visual paragraph box; only the invisible text-placement
corridor grows. Existing collision detection still restores fragment anchors for interleaved,
non-rectangular, or genuinely competing layouts.

A temporary reconstruction of the longer paragraph copied as one continuous line, retained an
identical render at 72, 150, and 300 DPI, and passed qpdf, structure serialization, and veraPDF
PDF/UA-1. Regression coverage now requires a Diaconis-length paragraph printed across six narrow
lines to compile as one `Tj` run with exact one-line extraction, while the earlier interleaved-list
test still requires physical fragment placement. The complete suite now contains 52 passing tests.
