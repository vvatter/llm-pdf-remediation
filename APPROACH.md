# LLM-First PDF Accessibility Remediation

## Purpose

This project remediates visually fixed historical PDFs for accessibility when the
original source documents are no longer available. It produces a new PDF that preserves
the visible document while adding machine-readable text, semantic structure, logical
reading order, document metadata, and alternative text.

The central design decision is that the large language model is authoritative for
content and meaning, while OCR and conventional PDF tools provide geometry, rendering,
and deterministic file construction.

In short:

- The LLM determines what the page says and how it should be read.
- OCRmyPDF determines where recognized words appear on the page.
- Python code converts that plan into a tagged PDF.
- Validators and Acrobat test whether the result behaves as intended.

Although the current examples are departmental newsletters, most of the architecture is
applicable to other fixed-layout archival documents.

## Goals

The pipeline is designed to:

1. Preserve the visible appearance of each document.
2. Provide accurate selectable and extractable Unicode text.
3. Establish an intentional reading order for multicolumn layouts.
4. Add headings, paragraphs, figures, alternative text, language, title metadata, and
   bookmarks.
5. Continue processing when a decision is uncertain while logging the uncertainty.
6. Produce repeatable output from a saved semantic plan.
7. Pass automated PDF syntax and PDF/UA-1 validation.
8. Work acceptably with real assistive technology, particularly Acrobat Read Out Loud.

The pipeline does not claim that automated validation alone proves complete WCAG 2.1 AA
conformance. Human review and assistive-technology testing remain acceptance checks.

## Non-Goals

The current proof of concept does not attempt to:

- Recreate the original publishing source.
- Visually redesign or modernize a document.
- Correct the authored wording of a newsletter.
- Produce an HTML alternative.
- Guarantee perfect transcription of every mathematical expression.
- Fully model complex tables, forms, lists, footnotes, or equations.
- Make Acrobat and every other PDF reader behave identically.

## Why LLM-First

Traditional OCR is useful, but it is not a sufficient authority for these documents.
Historical newsletters contain multiple columns, running headers, photographs, captions,
continuations, decorative rules, unusual fonts, and text that crosses column or page
boundaries. OCR commonly introduces errors such as:

- Incorrect characters and punctuation.
- Broken or retained end-of-line hyphenation.
- Decorative leader dots interpreted as digits or letters.
- Incorrect column order.
- Running headers inserted into article text.
- Captions and photographs interleaved with body paragraphs.

A vision-capable LLM is better suited to reconstructing the intended text and semantic
relationships from the rendered page. It can use embedded PDF text or OCR as supporting
evidence, but the page image remains authoritative.

OCR still has an important role. It is good at producing word bounding boxes and a
searchable geometric layer. The pipeline therefore treats OCR as a positioning sensor,
not as the final transcript.

## Division of Responsibility

### The LLM

The LLM is responsible for:

- Transcribing visible text.
- Rejoining line-broken and hyphenated words.
- Identifying headings, paragraphs, bylines, figures, and captions.
- Describing informative figures.
- Omitting decorative content.
- Selecting the logical reading order.
- Recognizing article and paragraph continuations.
- Returning confidence and ambiguity information.

The LLM returns structured JSON validated against Pydantic models. It does not write PDF
objects or content streams directly.

### OCRmyPDF and Existing PDF Text

OCRmyPDF and any usable native text layer are responsible for:

- Supplying word-level page coordinates.
- Providing evidence that helps align LLM words with the visible page.
- Producing a raster base when legacy visible fonts prevent reliable extraction or
  PDF/UA validation.
- Preserving a selectable geometric relationship between corrected text and the page.

The raw OCR transcript is not retained as the accessible transcript. Its text form is
suppressed after its geometry has been extracted.

### Deterministic Python Code

The Python compiler is responsible for:

- Normalizing the LLM plan.
- Choosing between original and OCR geometry page by page.
- Aligning corrected words to page coordinates.
- Assigning MCIDs.
- Constructing the structure tree and parent tree.
- Embedding the accessibility font and Unicode maps.
- Marking visible page content as an artifact.
- Adding metadata, language, bookmarks, and page tab order.
- Saving a syntactically valid, linearized PDF.

This boundary is important. LLM output may vary, but PDF syntax and object relationships
must be deterministic and internally consistent.

## Pipeline Overview

```text
Original PDF
    |
    +--> Rendered page images ----------------------+
    |                                               |
    +--> Native text blocks, when usable --------+  |
                                                |  |
                                                v  v
                                          LLM semantic plan
                                                |
                                                v
                                      Normalized document plan
                                                |
    +--> OCRmyPDF raster base --> OCR word boxes +
    |                                           |
    +--> Original word boxes -------------------+
                                                |
                                                v
                                      Per-page geometry choice
                                                |
                                                v
                                    Corrected word-box alignment
                                                |
                                                v
                                      Deterministic PDF compiler
                                                |
                                                v
                                      Remediated tagged PDF
                                                |
                              +-----------------+-----------------+
                              |                 |                 |
                            qpdf            veraPDF          Acrobat tests
```

## Stage 1: Page Extraction

Each page is rendered to a PNG at the configured planning DPI. The page packet contains:

- Page number.
- PDF page width and height.
- A base64-encoded rendered image.
- Compact native text blocks, when PyMuPDF can recover them.

The embedded text is explicitly described to the LLM as advisory. This matters because
the 1996 PDF contains Type 3 font data that produces unusable characters, while the 2004
PDF has substantially better native text.

## Stage 2: Structured LLM Planning

The OpenAI Responses API receives the page image and advisory text. It returns a
`PagePlan` containing ordered `PageElement` records.

The current semantic roles are:

- `DocumentTitle`
- `H1`
- `H2`
- `H3`
- `P`
- `Figure`

Each element contains:

- Exact accessible text, or figure alternative text.
- An approximate bounding box.
- A confidence score.
- An optional ambiguity message.

Page plans are checkpointed as JSON. A failed or interrupted batch can resume without
reprocessing completed pages or making repeated API calls.

## Stage 3: Plan Normalization

The model plan is normalized before compilation. Current normalization includes:

- Keeping one document title.
- Removing recognized running headers on later pages.
- Removing decorative figures and ornaments.
- Converting repeated document-title roles into headings when appropriate.
- Removing decorative table-of-contents leader dots while retaining entry titles and
  page numbers.
- Merging conservative cross-column paragraph continuations.

Ambiguous decisions are logged rather than stopping processing.

### Cross-Column Continuations

A paragraph that begins in the left column and continues at the top of the right column
must remain one semantic paragraph. The current deterministic refinement merges adjacent
paragraph fragments only when:

- Both elements are paragraphs.
- The first fragment ends near the bottom of the left column.
- The next fragment starts near the top of the right column.
- The first fragment does not end with sentence-closing punctuation.
- The next fragment begins with lowercase text, or the first fragment ends with clear
  continuation punctuation.

Each automatic merge is recorded in the ambiguity log. The operation is idempotent, so
rerunning the pipeline does not repeatedly merge content.

## Stage 4: OCR Base Creation

The `--ocr` workflow creates or reuses a cached OCR base:

```sh
.venv/bin/remediate-pdf src/document.pdf --ocr
```

OCRmyPDF is currently run with forced OCR because it provides a consistent page-image
base and removes inherited legacy font problems from visible content. Image optimization
is enabled to control file growth. Lossy JBIG2 character substitution is not enabled.

Forced OCR has a cost: born-digital vector pages become raster pages, and photo-heavy
documents can become substantially larger. The optimized 2004 proof currently remains
larger than its original source for this reason.

## Stage 5: Per-Page Geometry Selection

The compiler does not assume OCR geometry is always better. For each page it compares:

- Word boxes extracted from the OCR base.
- Word boxes extracted from the original PDF.

Each candidate is aligned against the LLM transcript. The quality score gives greater
weight to coverage of corrected LLM words and lesser weight to precision of the geometry
source. The better-scoring source is selected page by page.

This produces the intended behavior for the examples:

- The 1996 native text is unusable, so all pages use OCR geometry.
- Most 2004 pages use the cleaner native geometry.
- A small number of 2004 pages use OCR geometry when it aligns better.

The visible page still comes from the OCR base. Geometry selection affects only the
positioning of the corrected invisible text.

## Stage 6: Word Alignment

The corrected LLM token sequence is aligned with the selected geometry token sequence
using `difflib.SequenceMatcher` and normalized token keys.

The alignment handles:

- Exact token matches.
- OCR replacements.
- OCR deletions.
- LLM insertions.
- Different token counts within a replaced region.

When corrected and OCR token counts differ, available OCR boxes are distributed across
the corrected tokens. Inserted words use a neighboring box as a fallback. This keeps the
pipeline moving, but unusual replacements may still require better region-aware alignment
in a future version.

## Stage 7: PDF Construction

The PDF is compiled with pikepdf.

### Visible Content

The original content of the selected base remains visually unchanged by the tagging
compiler. Its page content is wrapped as an artifact so assistive technology does not
read the visible raster image or any legacy text as meaningful content.

OCRmyPDF's uncorrected OCR form XObjects are emptied after their word geometry has been
extracted. This prevents Acrobat from falling back to Tesseract errors.

### Accessibility Font

The corrected invisible text uses an embedded Noto Sans TrueType font. The compiler
constructs:

- A Type 0 font resource.
- A CIDFontType2 descendant.
- Correct glyph widths.
- A CID-to-GID map.
- A `/ToUnicode` CMap.
- An embedded font program.

This avoids the compatibility and PDF/UA failures caused by unembedded Helvetica or
legacy fonts without Unicode maps.

### Word-Level Marked Content

Each corrected word has:

- Its own marked-content sequence.
- Its own MCID.
- Word-level `/ActualText` including a preserved separator.
- Invisible glyph content positioned at the selected word box.
- A parent-tree entry connecting the MCID to its semantic element.

The semantic structure remains paragraph-oriented. A `P`, heading, or `Figure` contains
an ordered array of marked-content references for its words. This gives Acrobat granular
selection while retaining paragraph-level navigation and semantics.

This design is the result of several iterations. Paragraph-sized or 160-character
`/ActualText` values produced correct extraction in some tools but caused Acrobat to
start in the middle of paragraphs, skip later lines, or treat large text ranges as
indivisible selections.

### Structure Tree

The document receives:

- A `StructTreeRoot`.
- One root `Document` structure element.
- Ordered semantic elements for headings, paragraphs, and figures.
- Word-level marked-content references beneath each semantic element.
- A page-indexed parent tree.
- `StructParents` values on every tagged page.
- `/Tabs /S` so annotation tab order follows structure order.

Figures include `/Alt` descriptions. The document catalog includes language and viewer
preferences, and the metadata includes the document title, language, and PDF/UA part.

## Stage 8: Validation

Validation is deliberately layered because no single checker covers all behavior.

### Render Comparison

The selected base and remediated output are rendered at a fixed DPI and hashed. Exact
hash equality confirms that adding the invisible layer and tags did not change the base
rendering.

When a new raster base is created or optimized, it is also visually compared with the
original PDF. Lossy optimization is accepted only when the rendered difference is
negligible and manual inspection is clean.

### qpdf

`qpdf --check` verifies basic PDF syntax, stream integrity, encryption state, and
linearization.

### Transcript Extraction

Every output page is extracted with `pdftotext -raw`, normalized for whitespace, and
compared with the saved LLM plan. This catches missing text, unexpected OCR fallbacks,
and word-boundary errors.

### veraPDF

veraPDF checks PDF/UA-1 conformance. The current 1996 and 2004 proof outputs pass all 106
rules in the installed veraPDF profile with zero failed checks.

### Acrobat and Assistive Technology

Automated PDF/UA validation does not guarantee that a particular reader will expose the
document perfectly. Acrobat Read Out Loud testing revealed issues that the validators did
not, including:

- Reading only a page number and skipping the body.
- Ignoring very large `/ActualText` values.
- Falling back to raw OCR gibberish.
- Joining or fragmenting fitted words.
- Treating paragraph chunks as indivisible selection ranges.
- Skipping content where one MCID covered several geometric word ranges.

Manual Acrobat testing therefore remains part of the acceptance process.

## Outputs

For an input such as `src/2004_newsletter.pdf`, the pipeline writes:

```text
build/2004_newsletter/
    2004_newsletter.ocr-base.pdf
    2004_newsletter.plan.json
    2004_newsletter.ambiguities.jsonl
    2004_newsletter.accessible.pdf
    2004_newsletter.validation.json
```

The repository also stores the combined veraPDF report at:

```text
build/verapdf-ua1.json
```

## Reproducibility and Failure Handling

The design favors resumable batch processing:

- Completed page plans are checkpointed.
- Ambiguities do not stop the batch.
- OCR bases are cached.
- Geometry selection is deterministic for a saved plan.
- Continuation merging is idempotent.
- PDF object generation does not depend on further model decisions.
- Validation reports are saved beside the output.

Deleting a plan JSON intentionally requests new LLM planning. Otherwise, reruns reuse the
existing semantic decisions.

## Lessons From the Proof of Concept

### OCR Should Be Evidence, Not Authority

OCRmyPDF produced strong selection geometry, but its transcript included obvious errors
such as decorative table-of-contents leaders interpreted as text. Retaining the LLM plan
as the canonical transcript fixed this class of problem.

### PDF/UA Validation and Reader Behavior Are Different

A structurally valid file can still behave poorly in Acrobat. Both technical validation
and assistive-technology testing are required.

### Granularity Matters

One hidden glyph per paragraph was too little. One large text run per paragraph was also
problematic. Chunk-level MCIDs improved some behavior but still allowed Acrobat to skip
geometric ranges. The current word-level MCID design is larger but substantially more
predictable.

### Reading Order Must Exist at Two Levels

The semantic elements must be in logical order, and the marked content within each
element must also be ordered. Correct headings and paragraphs are not sufficient when
their child content references traverse columns incorrectly.

### Native Geometry Can Be Better Than OCR Geometry

Born-digital PDFs may have accurate word boxes even when their font resources are not
PDF/UA compliant. Using native geometry with a raster visual base can improve selection
without reintroducing invalid fonts.

### File Size Is a Real Tradeoff

Forced OCR removes legacy font problems but rasterizes vector pages. Photograph-heavy
documents grow more than black-and-white text documents. Image optimization and direct
marked-content references reduce the overhead, but raster output will not generally match
the compact size of a well-constructed born-digital source.

## Newsletter-Specific Parts

The architecture is general, but the current implementation contains some
newsletter-specific policy:

- The system prompt describes historical newsletters.
- Running-header normalization recognizes example-specific wording.
- The semantic model focuses on headings, paragraphs, and figures.
- Continuation detection assumes a conventional left/right column layout.
- The CLI and output naming use newsletter terminology.

These policies could be moved into document profiles while retaining the shared planning,
alignment, compilation, and validation engine.

## Generalization

With configurable prompts and semantic schemas, the same approach could support:

- Magazines and journals.
- Departmental reports.
- Conference proceedings.
- Scanned books and pamphlets.
- Catalogs and brochures.
- Archival institutional records.

More complex document classes would require additional roles and compiler support for
tables, lists, forms, footnotes, links, quotations, and mathematical expressions.

## Recommended Next Improvements

The highest-value improvements are:

1. Introduce an explicit layout graph with `article_id`, `paragraph_id`, region, and
   continuation relationships.
2. Align text within detected lines and regions rather than across an entire page.
3. Add automated anomaly reports for backward reading jumps, column changes, overlaps,
   low alignment scores, and incomplete paragraph boundaries.
4. Re-plan only problematic pages with the existing plan and observed failure included in
   the prompt.
5. Add semantic support for lists, tables of contents, forms, captions, footnotes, and
   mathematics.
6. Investigate visible-font reconstruction for born-digital documents where file size is
   more important than the simplicity of rasterization.
7. Establish a larger regression corpus with manual Acrobat test notes for different page
   layouts.

## Reference Material

- [OCRmyPDF documentation](https://ocrmypdf.readthedocs.io/)
- [PDF Association Techniques for Accessible PDF](https://pdfa.org/techniques-for-accessible-pdf/)
- [PDF Association Tagged PDF Q and A](https://pdfa.org/resource/tagged-pdf-q-a/)
- [PDF Association Tagged PDF Best Practice Guide](https://pdfa.org/resource/tagged-pdf-best-practice-guide-syntax/)
