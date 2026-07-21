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
   leader/marker omission, inline-list separator omission, structural separator normalization, or whitespace
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
                           canonical schema-v5 plan
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
- **Native:** at least 95% of nonblank pages have usable native text and all used fonts
  are embedded with a reliable encoding. The source page objects are preserved.
- **Hybrid:** the fonts are embedded and reliably encoded but native Unicode coverage is
  incomplete. OCR supplies evidence and geometry while the source page objects remain
  the visible compilation base.
- **Facsimile:** the PDF renders but legacy fonts, mappings, or text extraction make the
  native content unsafe. OCRmyPDF creates an optimized raster base.
- **Unsupported:** encryption, structural damage, or render failure prevents safe work.

An operator can override the selected mode, and the override is recorded. The extraction
compatibility and visual-fidelity gates still measure the result.

Native and hybrid modes preserve the source page objects. Facsimile mode uses OCRmyPDF
with forced OCR, 300 DPI oversampling, and conservative image optimization. OCRmyPDF may
continue past a recoverable soft image-rendering error in a malformed legacy source;
the mandatory source-to-base render comparison still prevents a visually changed base
from release. Lossy JBIG2 substitution is not enabled. OCR text is evidence and geometry,
not the final transcript.

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

Schema version 5 gives every element a stable `pNNNN-eNNNN` identifier, gives every
rectangular visual block a stable identifier, and records:

- visible fragments, their normalized boxes, and their evidence references;
- page flows that own every visual block exactly once in reading order;
- exact visible and accessible text;
- declared transformations, including atomic whole-expression formula speech;
- semantic role and normalized bounding box;
- four confidence dimensions;
- native, OCR, model-agreement, and alignment evidence;
- findings, alternatives, and chosen readings;
- review status;
- deterministic word offsets.

Page 1 may also carry a reviewed `document_title_candidate`. It is PDF metadata rather
than printed transcription. The reviewer chooses it for concise usefulness in browser
tabs and search results, using only page-supported context and relevant examples from
the user's ignored rolling recent-title file. No particular metadata field is required.

The `LI` role represents one item in an explicit list or roster. Consecutive `LI`
elements form a list; headings and paragraphs create explicit list boundaries. A compact
comma-separated roster may still be a list, but ordinary prose with a series remains a
paragraph. Deterministic refinement changes isolated unmarked `LI` entries to `P` so a
screen reader is not asked to announce a one-item list.

Heading roles describe the visible document's semantic outline rather than its typography.
A heading must be visible text that introduces the content that follows. The models may split an
explicit inline section label from its body text when the label has an exact visual fragment, but
they may not invent an accessibility-only section name for unlabeled content. Short metadata and
key-value labels remain paragraph content unless they genuinely introduce subsections. `H1`,
`H2`, and `H3` express nesting rather than font size, and an `H3` requires a real parent `H2`.

Logical elements and visual blocks are deliberately separate. A paragraph that begins
at the bottom of one column and continues at the top of another remains one `P`, but it
has two ordered rectangular fragments. A fragment may not span disjoint columns. The
page flow makes reading order explicit even when global top-to-bottom or left-to-right
coordinates would interleave independent articles, sidebars, or contents boxes.

The direct-Unicode accessibility layer normally collapses visual wrapping within a logical
semantic line. If fitting a long logical line inside its narrow printed paragraph box would make
word spaces too small for reliable extraction, the invisible run first expands into otherwise
unused horizontal page space. Expansion stays inside the page and stops before any other semantic
run on the same baseline; the structure element's `/Layout /BBox` continues to describe only the
true printed paragraph. Before compilation, the resulting run rectangles are compared across
elements. If de-wrapping would make two invisible runs overlap on the same baseline, or no safe
corridor can preserve usable word spacing, the affected element is instead anchored fragment by
fragment at its non-overlapping printed positions while remaining a single structure element.
Structure-level `ActualText` retains its reviewed de-wrapped string for viewers that honor it. A
collision that remains after fragment-aware placement blocks compilation rather than allowing
spatial text extractors to interleave characters.
If a model returns flow ownership or order inconsistent with its own ordered elements,
the parser discards only that invalid grouping, derives one flow from semantic element
and fragment order, and records an informational reading-order finding. Transcription,
roles, fragments, and element order remain unchanged.

Every page declares its coordinate space. Model output is constrained to
`normalized_0_1000`, OCR/native evidence boxes are normalized before prompting, and
legacy schema-v2 point boxes are deterministically migrated. The compiler replaces
non-figure element boxes with the union of aligned word evidence when available.

Artifacts are explicit records with a bounding box and reason. The deterministic
post-review pass currently identifies printed page numbers, repeated top/bottom
furniture, writing lines, and small decorative figures. These items remain visible in
the page facsimile but do not enter the semantic reading stream. A small empty-alt
figure is treated as decoration only when the reviewed findings explicitly classify it
that way; other empty-alt figures remain release-blocking errors.

On the first page, an attributed epigraph is placed after publication and
volume/issue/date metadata and before the first article heading. Proposal/review prompt
version 9 states this policy, the title/list and evidence-based heading policies, the
atomic inline-formula contract, and the rule that only an unresolved canonical defect
receives critical severity. The deterministic refinement applies the same narrow ordering
rule to existing reviewed plans while recording an informational reading-order finding.

Word offsets are computed from the exact accessible string. For each non-whitespace
token, the model stores `start`, `end`, and `actual_end`, where `actual_end` reaches to
the next token. Concatenating `text[start:actual_end]` exactly reconstructs punctuation,
newlines, nonbreaking spaces, and other joiners. This replaces the earlier practice of
appending a generic space to every word.

Schema-v1 and schema-v4 plans migrate automatically and are marked `legacy_unreviewed`.
Reviewed schema-v2 and schema-v3 plans retain their approval while their geometry and default
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
- groups corrected tokens into measured visual lines for alignment and region geometry,
  with a bounded synthetic layout when a region has no sufficiently aligned source words;
- emits corrected Unicode directly as one invisible `Tj` run per explicit semantic line
  in the canonical transcript, ignoring visual line wrapping inside that semantic line;
- emits one semantic marked-content sequence and MCID per connected visual region;
- emits each reviewed visual region as a separate page content stream containing one
  invisible `BT`/`ET` text object;
- preserves normalized mathematical notation in the copy/search layer while wrapping
  each exact inline expression in a `/Formula` child with the independently reviewed
  spoken alternative in `/Alt`; formula and prose segments share the same logical line
  so Formula boundaries do not introduce extraction breaks;
- maps tab and newline controls as zero-width Unicode codes so exact joiners remain
  direct text rather than region-wide replacements;
- uses `/ActualText` only as a fallback when the embedded font cannot represent an
  exceptional character;
- creates headings, figures, and single-block paragraphs directly under `/Document`;
- groups consecutive list items as `/L` containers with `/LI` and `/LBody` descendants;
- emits each spatially disjoint region of a multi-block paragraph as a consecutive
  direct `/P` because Acrobat otherwise reverses or loses fragment click targets;
- stores ordinary regions as a single same-page MCID and formula-bearing regions as an
  ordered mixture of parent-owned text MCIDs and `/Formula` children;
- represents each meaningful figure with a nonpainting rectangle MCID, structural
  `/Alt`, and `/Layout /BBox` instead of duplicating the description as hidden text;
- records each text region's actual word-union box as a `/Layout /BBox` attribute;
- keeps plan and fragment IDs in canonical JSON rather than emitting PDF structure
  element `/ID` entries that would require a complete `/IDTree`;
- builds the ParentTree and page `StructParents` values;
- adds `/Tabs /S`, document language, title, viewer preference, and bookmarks.
- adds decimal PDF page labels matching the printed pagination.

The ordinary region-level MCID, plus inline formula MCIDs inside the same direct Unicode
line object, is an Acrobat compatibility profile rather than part of the semantic plan.
This strategy is recorded in the manifest so another compiler profile can be compared
later without replanning the document.

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

Constrained evidence-repair paths remain for invalid or visibly shifted fragment boxes.
A fragment may recover its box from ordered page words only when at least half of its
visible tokens match. A valid but poorly aligned box may move only to a match with a
roughly similar footprint, preventing repeated words in unrelated regions from producing
a page-wide union. A one- or two-token fragment may move only when its exact sequence has
one unique page occurrence. The recovered word union becomes the new fragment box, after
which ordinary block-local alignment and validation apply.

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
8. Requires nonempty text elements and alternate text for figures and formulas.
9. Compares role, exact extractable notation, formula alternatives, and element text
   with the canonical plan.
10. Verifies visual-block identifiers, single flow ownership, exact flow order, bounded
    block geometry, and completed block-local alignment evidence.
11. Reconstructs accessible text from exact transformation source/target spans.
12. Compares Poppler `pdftotext -raw` tokens with the canonical transcript and rejects
    duplicated or substantially missing ordinary extraction.
13. Samples original-to-base renders at 72 and 150 DPI and requires every page's mean
    normalized channel difference to be at most 0.052 and material-difference fraction
    to be at most 0.25.

Only after these checks does a temporary candidate receive `pdfuaid:part=1` and its
release provenance. The project is recorded as the PDF producer, while misleading
intermediate OCR/PDF-library Creator values are removed. Preflight snapshots source
authors, Subject, Keywords, XMP description, creation date, and encoding applications
before derivative tools run; release restores the content metadata and original creation
date. The `llmpr` XMP namespace records the tool and version, UTC remediation date,
schema version, source SHA-256, canonical-plan SHA-256, original encoding software, and
a brief remediation summary crediting ChatGPT 5.6 Sol. This keeps production history out
of content-description fields while allowing an inventory crawler to recognize outputs
deterministically. The two human-facing provenance values are mirrored into custom PDF
Info properties so Acrobat displays them in its Custom document-properties tab. The
release gate requires the Info and XMP copies to agree before veraPDF runs its PDF/UA-1
profile. The accessible output path is published only if all machine checks pass. On
failure, the temporary candidate is discarded and the undeclared draft plus validation
report remain.

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

## Proven Corpus

The initial regression corpus contains nine historical PDFs. All nine completed the
facsimile workflow and passed the project's release gates, including veraPDF PDF/UA-1.
Three representative outputs also received successful manual Acrobat Read Out Loud,
clicking, selection, and reading-order spot checks. The remaining six have machine
evidence but still need document-level assistive-technology testing.

This corpus exercises scan-like pages, damaged or missing character mappings,
multi-column articles, nested columns, paragraph continuations, contents boxes,
photographs, captions, decorative objects, a blank page, and interleaved label/value
layouts. Exact results and the changes prompted by failures are recorded in
[DEVELOG.md](DEVELOG.md).

## Current Boundaries

The present logical roles are `DocumentTitle`, `H1`, `H2`, `H3`, `P`, `LI`, and `Figure`.
Inline formula semantics are derived from exact `formula_spoken_equivalent`
transformations: notation remains available to copy/search while `/Formula` children
carry spoken alternatives. The next semantic layer should add a document-level article
graph plus captions, table-of-contents entries, display-math structure, quotations, and
references. Explicit artifacts and page labels are now implemented.

Other deferred work includes region/line dynamic-programming alignment, PAC automation,
formal NVDA/JAWS scripts, native image-object tagging, richer PDF/UA-2 math interchange,
and a
repeatable full-corpus rebuild harness. An interactive human-remediation stage is
explicitly out of scope; these additions can extend the saved plan and compiler without
changing the central independently model-reviewed plan boundary.
