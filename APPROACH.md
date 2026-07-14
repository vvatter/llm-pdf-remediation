# Remediation Architecture

## Purpose and Trust Model

The pipeline remediates historical fixed-layout PDFs whose publishing sources no longer
exist. It creates a new PDF with machine-readable text, semantic structure, reading
order, metadata, and alternative text while preserving the selected visible page base.
It does not produce HTML or visually redesign the document.

The canonical, saved plan is authoritative. No individual model response is.

The data model distinguishes:

1. **Visible transcription:** what is printed, including authored spelling and errors.
2. **Accessible text:** the spoken form, normally identical to the visible text.
3. **Declared transformations:** narrow mechanical changes such as line-break
   dehyphenation, ligature expansion, soft-hyphen removal, formula speech, decorative
   leader/marker omission, structural separator normalization, or whitespace
   normalization.
4. **Canonical reviewed plan:** the exact input accepted by the deterministic compiler.

Each element records four confidence dimensions: transcription, semantic role,
geometry, and reading order. Native text, OCR, proposal/reviewer agreement, model
findings, alternatives, and transformations remain provenance rather than being folded
into one self-reported confidence number.

The plan currently follows visual page order. A future document-level article graph may
support article-by-article traversal, but it is intentionally not inferred by this
foundation release.

## Pipeline

```text
source PDF
    |
    +--> preflight --> pass-through / native / facsimile / unsupported
    |
    +--> page image + native evidence + OCR evidence
                    |
                    +--> Terra proposal (medium reasoning)
                    |       saved unchanged
                    |
                    +--> deterministic disagreement diagnostics
                    |
                    +--> Sol review (high reasoning)
                            image and evidence first
                            diagnostics and proposal second
                            always chooses a canonical page
                                    |
                                    v
                           canonical schema-v4 plan
                                    |
                 selected visible base + geometry
                                    |
                                    v
                         deterministic PDF compiler
                                    |
                                    v
                         undeclared tagged draft
                                    |
                render + qpdf + structure transcript gates
                                    |
                         temporary PDF/UA candidate
                                    |
                              veraPDF UA-1
                                    |
                          published accessible PDF
```

## Preflight and Visible Base

Preflight records the source SHA-256, encryption state, qpdf result, renderability, page
count, blank pages, native text coverage, invalid-character ratios, font embedding,
font encodings, existing tags, and existing PDF/UA validity.

Automatic classification and execution are conservative:

- **Pass-through:** the input already has tags and passes veraPDF PDF/UA-1. It is
  validated and left byte-for-byte unchanged.
- **Native candidate:** at least 95% of nonblank pages have usable native text and all
  used fonts are embedded with a reliable encoding. The current batch-safe policy still
  compiles this as facsimile unless `--native-experimental` is supplied, because the
  existing native text is not yet incorporated into the new structure tree and ordinary
  extractors otherwise report it in addition to the semantic anchors.
- **Facsimile:** the PDF renders but legacy fonts, mappings, or text extraction make the
  native content unsafe. OCRmyPDF creates an optimized raster base.
- **Unsupported:** encryption, structural damage, or render failure prevents safe work.

An operator can override the selected mode, and the override is recorded. Experimental
native mode requires both `--mode native` and `--native-experimental`; the extraction
compatibility gate still measures the result.

Native mode preserves the source page objects. Facsimile mode uses OCRmyPDF with forced
OCR, 300 DPI oversampling, and conservative image optimization. Lossy JBIG2 substitution
is not enabled. OCR text is evidence and geometry, not the final transcript.

Rasterization can implicate WCAG 2.1 criteria 1.4.5 (Images of Text) and 1.4.10
(Reflow). The evidence matrix therefore leaves facsimile rationale, high magnification,
and reflow as explicit review work rather than treating PDF/UA syntax as proof.

## Proposal and Independent Review

The proposal stage sends the page image plus compact native and OCR text to
`gpt-5.6-terra` with medium reasoning. It returns visible text, accessible text,
transformations, roles, approximate boxes, confidence dimensions, and findings.

The review stage sends `gpt-5.6-sol` with high reasoning:

1. The page image.
2. Native and OCR evidence.
3. Deterministic agreement diagnostics and sensitive names, dates, numbers, URLs, and
   formula-like tokens.
4. The first model's proposal.

The reviewer must choose a canonical result even when the page is ambiguous. Critical
findings are advisories and do not stop batch processing. There is no model fallback:
an unavailable configured model produces a clear failed run.

Evidence, proposal, and review files are independent checkpoints. Re-running resumes at
the missing stage, so a completed proposal is not paid for or regenerated when only its
review is missing. API response IDs, model IDs, reasoning effort, prompt versions, and
source/page hashes are retained in the build record.

## Canonical Schema

Schema version 4 gives every element a stable `pNNNN-eNNNN` identifier, gives every
rectangular visual block a stable identifier, and records:

- visible fragments, their normalized boxes, and their evidence references;
- page flows that own every visual block exactly once in reading order;
- exact visible and accessible text;
- declared transformations;
- semantic role and normalized bounding box;
- four confidence dimensions;
- native, OCR, model-agreement, and alignment evidence;
- findings, alternatives, and chosen readings;
- review status;
- deterministic word offsets.

Logical elements and visual blocks are deliberately separate. A paragraph that begins
at the bottom of one column and continues at the top of another remains one `P`, but it
has two ordered rectangular fragments. A fragment may not span disjoint columns. The
page flow makes reading order explicit even when global top-to-bottom or left-to-right
coordinates would interleave independent articles, sidebars, or contents boxes.

Every page declares its coordinate space. Model output is constrained to
`normalized_0_1000`, OCR/native evidence boxes are normalized before prompting, and
legacy schema-v2 point boxes are deterministically migrated. The compiler replaces
non-figure element boxes with the union of aligned word evidence when available.

Artifacts are explicit records with a bounding box and reason. The deterministic
post-review pass currently identifies printed page numbers, repeated top/bottom
furniture, writing lines, and small decorative figures. These items remain visible in
the page facsimile but do not enter the semantic reading stream.

On the first page, an attributed epigraph is placed after publication and
volume/issue/date metadata and before the first article heading. Proposal/review prompt
version 5 states this policy, and the deterministic refinement applies the same narrow
rule to existing reviewed plans while recording an informational reading-order finding.

Word offsets are computed from the exact accessible string. For each non-whitespace
token, the model stores `start`, `end`, and `actual_end`, where `actual_end` reaches to
the next token. Concatenating `text[start:actual_end]` exactly reconstructs punctuation,
newlines, nonbreaking spaces, and other joiners. This replaces the earlier practice of
appending a generic space to every word.

Schema-v1 plans migrate automatically and are marked `legacy_unreviewed`. Reviewed
schema-v2 and schema-v3 plans retain their approval while their geometry and default
page flows are migrated.
Original JSON is backed up. The next ordinary run can use each schema-v1 page as a
proposal and put it through the independent review stage.

## Deterministic Compilation

The compiler uses pikepdf and fontTools. It never asks a model to produce PDF syntax.
It:

- preserves the selected visible page content and marks it as an artifact;
- suppresses uncorrected OCR text form streams after extracting their geometry;
- embeds an open TrueType font as a Type 0/CIDFontType2 resource;
- creates explicit widths, a CID-to-GID map, and `/ToUnicode` mapping;
- partitions canonical tokens by their model-reviewed visual fragments;
- aligns each fragment only to native or OCR words inside that fragment's box;
- retains OCR/native block and line identifiers through corrected-token alignment;
- groups corrected tokens into measured visual lines, with a bounded synthetic line
  layout when a region has no sufficiently aligned source words;
- emits corrected Unicode text directly in invisible line-level `Tj` strings;
- emits one semantic marked-content sequence and MCID per connected visual region;
- emits each reviewed visual region as a separate page content stream containing one
  invisible `BT`/`ET` text object;
- maps tab and newline controls as zero-width Unicode codes so exact joiners remain
  direct text rather than region-wide replacements;
- uses region-wide `/ActualText` only when the embedded font cannot represent an
  exceptional character;
- creates headings, figures, and single-block paragraphs directly under `/Document`;
- emits each spatially disjoint region of a multi-block paragraph as a consecutive
  direct `/P` because Acrobat otherwise reverses or loses fragment click targets;
- stores each region's single same-page MCID as the integer child of its structure
  element;
- represents each meaningful figure with a nonpainting rectangle MCID, structural
  `/Alt`, and `/Layout /BBox` instead of duplicating the description as hidden text;
- records each text region's actual word-union box as a `/Layout /BBox` attribute;
- keeps plan and fragment IDs in canonical JSON rather than emitting PDF structure
  element `/ID` entries that would require a complete `/IDTree`;
- builds the ParentTree and page `StructParents` values;
- adds `/Tabs /S`, document language, title, viewer preference, and bookmarks.
- adds decimal PDF page labels matching the printed pagination.

The region-level MCID with direct line Unicode is an Acrobat compatibility profile, not
part of the semantic plan. This strategy is recorded in the manifest so another compiler
profile can be compared later without replanning the document.

The visual blocks remain authoritative plan, geometry, and text-object units. They do
not introduce extra `/Div` or `/Span` structure wrappers. Nearby or overlapping blocks,
such as a drop cap and its body text, form one connected region. Spatially disjoint
continuations become consecutive direct `/P` regions. Validation groups those regions
back into the canonical logical element and requires their concatenated exact text to
match the plan. This is an explicit Acrobat compatibility tradeoff: the extra structural
boundary produces a longer speech pause, but sharing a `/P` parent made the first
fragment unclickable and caused Acrobat to read the second fragment first. Region-scoped
streams keep physical content order identical to structure order and provide independent
click geometry. The compiler splits a multi-block paragraph only when concatenating the
resulting spatial regions exactly reconstructs the approved transcript. Interleaved
label/value columns instead remain one ordered region so spatial grouping cannot corrupt
their semantic order.

Page-wide agreement is used only to choose the better native or OCR geometry source.
Token alignment then uses `SequenceMatcher` with `autojunk=False` separately inside
each reviewed visual block. Source word block/line identifiers survive alignment into
the corrected tokens, including replacements that inherit nearby evidence. If a block
has no sufficiently aligned source words, its corrected text is wrapped into short
synthetic lines inside that block rather than borrowed from another column. A future
weighted aligner may improve difficult formulas and insertions, but global page
alignment no longer controls anchor placement.

One constrained legacy-repair path remains: a zero-area migrated block may recover its
box from page words only when at least half of its visible tokens match. The recovered
word union becomes the new block box, after which ordinary block-local alignment and
validation apply. Valid reviewed boxes never take this path.

## Validation and Release Gates

The draft deliberately omits the PDF/UA identification metadata. Validation then:

1. Renders the selected base and draft and compares exact pixel hashes.
2. Runs `qpdf --check`.
3. Parses marked-content operators on every page.
4. Checks MCID uniqueness and balanced marked-content sequences.
5. Walks the structure tree in order.
6. Resolves every integer MCID or MCR and decodes direct Type 0 Unicode text, falling
   back to `/ActualText` where explicitly present.
7. Verifies every ParentTree entry and detects missing, duplicate, or orphan MCIDs.
8. Requires nonempty text elements and alternate text for figures.
9. Compares role and exact element text with the canonical plan.
10. Verifies visual-block identifiers, single flow ownership, exact flow order, bounded
    block geometry, and completed block-local alignment evidence.
11. Reconstructs accessible text from exact transformation source/target spans.
12. Compares Poppler `pdftotext -raw` tokens with the canonical transcript and rejects
    duplicated or substantially missing ordinary extraction.
13. Samples original-to-base renders at 72 and 150 DPI and requires every page's mean
    normalized channel difference to be at most 0.052 and material-difference fraction
    to be at most 0.25.

Only after these checks does a temporary candidate receive `pdfuaid:part=1`. veraPDF
then runs its PDF/UA-1 profile. The accessible output path is published only if all
machine checks pass. On failure, the temporary candidate is discarded and the
undeclared draft plus validation report remain.

The structure serializer is the deterministic reading-order test. `pdftotext` is also a
release compatibility gate because it exposed native-layer duplication. Acrobat Read
Out Loud remains a useful compatibility regression, but neither proves that assistive
technology will traverse the tag tree correctly.

## Reports and Acceptance Evidence

Every build produces:

- JSONL findings for automation;
- a self-contained, read-only HTML anomaly packet showing only flagged pages;
- a WCAG 2.1 AA criterion matrix;
- a build manifest with source and page hashes, plan hash, model response IDs, prompt
  versions, OCR settings, tool versions, font hash, git commit, mode, and compiler
  strategy.

The WCAG matrix distinguishes `pass`, `fail`, `review`, `not_tested`, and
`not_applicable`. Machine-valid PDF/UA is evidence for several criteria, not a claim that
all WCAG 2.1 AA requirements have been met.

Institutional acceptance should still include NVDA with Acrobat Reader on Windows,
optionally JAWS, heading and figure navigation, continuous reading, search, selection,
copy/paste, 400% zoom, narrow-window reflow, and page navigation. Findings remain logged
for audit and future model improvements; they do not pause the unattended build.

## Current Boundaries

The present roles are `DocumentTitle`, `H1`, `H2`, `H3`, `P`, and `Figure`. The next
semantic layer should add a document-level article graph plus captions, lists, table of
contents entries, formulas, quotations, and references. Explicit artifacts and page
labels are now implemented.

Other deferred work includes region/line dynamic-programming alignment, PAC automation,
formal NVDA/JAWS scripts, native image-object tagging, mathematics font fallback, and a
larger golden regression corpus. An interactive human-remediation stage is explicitly
out of scope; these additions can extend the saved plan and compiler without changing
the central independently model-reviewed plan boundary.
