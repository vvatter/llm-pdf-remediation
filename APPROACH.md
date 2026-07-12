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
   leader omission, or whitespace normalization.
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
                           canonical schema-v2 plan
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

Automatic classification is conservative:

- **Pass-through:** the input already has tags and passes veraPDF PDF/UA-1. It is
  validated and left byte-for-byte unchanged.
- **Native:** at least 95% of nonblank pages have usable native text and all used fonts
  are embedded with a reliable encoding.
- **Facsimile:** the PDF renders but legacy fonts, mappings, or text extraction make the
  native content unsafe. OCRmyPDF creates an optimized raster base.
- **Unsupported:** encryption, structural damage, or render failure prevents safe work.

An operator can override the selected mode, and the override is recorded. This is useful
for unusual files but does not erase the failed automatic criteria.

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

Schema version 2 gives every element a stable `pNNNN-eNNNN` identifier and records:

- visible fragments and their evidence references;
- exact visible and accessible text;
- declared transformations;
- semantic role and normalized bounding box;
- four confidence dimensions;
- native, OCR, model-agreement, and alignment evidence;
- findings, alternatives, and chosen readings;
- review status;
- deterministic word offsets.

Word offsets are computed from the exact accessible string. For each non-whitespace
token, the model stores `start`, `end`, and `actual_end`, where `actual_end` reaches to
the next token. Concatenating `text[start:actual_end]` exactly reconstructs punctuation,
newlines, nonbreaking spaces, and other joiners. This replaces the earlier practice of
appending a generic space to every word.

Schema-v1 plans migrate automatically and are marked `legacy_unreviewed`. Their original
JSON is backed up. The next ordinary run can use each legacy page as a proposal and put
it through the independent review stage.

## Deterministic Compilation

The compiler uses pikepdf and fontTools. It never asks a model to produce PDF syntax.
It:

- preserves the selected visible page content and marks it as an artifact;
- suppresses uncorrected OCR text form streams after extracting their geometry;
- embeds an open TrueType font as a Type 0/CIDFontType2 resource;
- creates explicit widths, a CID-to-GID map, and `/ToUnicode` mapping;
- aligns canonical tokens to native or OCR word boxes;
- emits invisible glyphs at those boxes;
- emits one marked-content sequence and MCID per word;
- places the exact word plus its original following joiner in `/ActualText`;
- creates paragraph- or heading-level structure elements owning ordered MCRs;
- builds the ParentTree and page `StructParents` values;
- adds `/Tabs /S`, document language, title, viewer preference, and bookmarks.

The word-level strategy is an Acrobat compatibility profile, not part of the semantic
plan. It is recorded in the manifest so another compiler strategy can be compared later
without replanning the document.

The existing alignment uses page-wide `SequenceMatcher` with `autojunk=False`. It is
adequate for the current proof corpus, but region, line, and weighted token alignment is
deferred. Inserted text still uses nearby geometry and is surfaced through evidence and
review findings rather than silently being treated as exact geometry.

## Validation and Release Gates

The draft deliberately omits the PDF/UA identification metadata. Validation then:

1. Renders the selected base and draft and compares exact pixel hashes.
2. Runs `qpdf --check`.
3. Parses marked-content operators on every page.
4. Checks MCID uniqueness and balanced marked-content sequences.
5. Walks the structure tree in order.
6. Resolves every MCR, page, and `/ActualText` value.
7. Verifies every ParentTree entry and detects missing, duplicate, or orphan MCIDs.
8. Requires nonempty semantic elements and alternate text for figures.
9. Compares role and exact element text with the canonical plan.

Only after these checks does a temporary candidate receive `pdfuaid:part=1`. veraPDF
then runs its PDF/UA-1 profile. The accessible output path is published only if all
machine checks pass. On failure, the temporary candidate is discarded and the
undeclared draft plus validation report remain.

The structure serializer is the deterministic reading-order test. `pdftotext` and
Acrobat Read Out Loud remain useful compatibility regressions, but neither proves that
assistive technology will traverse the tag tree correctly.

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
copy/paste, 400% zoom, narrow-window reflow, page navigation, and human review of names,
dates, formulas, alternative text, and high-severity findings.

## Current Boundaries

The present roles are `DocumentTitle`, `H1`, `H2`, `H3`, `P`, and `Figure`. The next
semantic layer should add a document-level article graph plus captions, lists, table of
contents entries, formulas, quotations, references, explicit artifacts, and page labels.

Other deferred work includes region/line dynamic-programming alignment, a write-back
human reviewer, PAC automation, formal NVDA/JAWS scripts, native image-object tagging,
mathematics font fallback, and a larger golden regression corpus. These additions can
extend the saved plan and compiler without changing the central reviewed-plan boundary.
