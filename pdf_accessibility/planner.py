from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from .extract import PagePacket
from .models import DocumentPlan, ElementRole, PagePlan


SYSTEM_PROMPT = """You remediate historical fixed-layout PDFs for screen-reader access.
Transcribe and semantically structure the supplied page without changing, summarizing,
modernizing, or correcting its authored wording. The page image is authoritative.
Embedded text is advisory and may contain incorrect characters or reading order.

Return every meaningful item in exact screen-reader reading order. Use:
- DocumentTitle only for the document title on its first page.
- H1 for article titles, H2/H3 for real nested headings, P for body text, bylines,
  continuations, contents entries, and other meaningful text.
- Figure for informative photographs, illustrations, charts, or graphical quotations.
  Give each Figure concise alt text; do not transcribe text inside a Figure into alt text
  when it also needs to be read as normal text.
- Omit decorative rules, ornaments, and repeated running headers/footers. Include a page
  number only when it helps preserve the newsletter's explicit pagination.

Join line-broken and hyphenated body copy into natural paragraphs. Preserve intentional
punctuation, names, dates, mathematical notation, article continuations, and column order.
Use normalized 0..1000 coordinates for approximate bounding boxes. Assign confidence to
each decision. Record ambiguity, but always choose a result and continue."""


def _page_prompt(packet: PagePacket) -> str:
    candidate = packet.embedded_text or "(No usable embedded text was recovered.)"
    return f"""Analyze document page {packet.page_number}.
Page size: {packet.width:.1f} by {packet.height:.1f} PDF points.

Candidate embedded blocks follow. They may be accurate, partially encoded, or garbage.
Use them to verify spelling only when they agree with the rendered page:

{candidate}
"""


def plan_page(client: OpenAI, model: str, packet: PagePacket) -> PagePlan:
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _page_prompt(packet)},
                    {
                        "type": "input_image",
                        "image_url": packet.image_data_url,
                        "detail": "high",
                    },
                ],
            },
        ],
        text_format=PagePlan,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"model returned no parsed plan for page {packet.page_number}")
    plan = response.output_parsed
    plan.page_number = packet.page_number
    return plan


def infer_title(source: Path, pages: list[PagePlan]) -> str:
    for page in pages:
        for element in page.elements:
            if element.role == ElementRole.DOCUMENT_TITLE:
                return element.text.strip()
    return source.stem.replace("_", " ").strip().title()


def normalize_document_pages(source: Path, pages: list[PagePlan]) -> list[PagePlan]:
    canonical_title = infer_title(source, pages)
    kept_document_title = False
    normalized_title = " ".join(canonical_title.lower().split())

    for page in pages:
        cleaned = []
        for element in page.elements:
            if element.text.lower().startswith(("inside this issue", "contents ")):
                element.text = re.sub(r"(?:\s*\.\s*){3,}", " ", element.text)
                element.text = " ".join(element.text.split())
            text = " ".join(element.text.lower().split())
            is_running_header = (
                (normalized_title and normalized_title in text and "spring" in text)
                or "univ of florida mathematics newsletter" in text
                or "department of mathematics newsletter" in text and page.page_number > 1
                or text.startswith("vol 18. no 1.")
            )
            if element.role == ElementRole.DOCUMENT_TITLE:
                if page.page_number == 1 and not kept_document_title:
                    kept_document_title = True
                elif is_running_header:
                    continue
                else:
                    element.role = ElementRole.H1
            elif is_running_header and element.bbox[1] < 180:
                continue

            if (
                element.role == ElementRole.FIGURE
                and element.alt_text
                and any(word in element.alt_text.lower() for word in ("decorative", "flourish", "ornament"))
            ):
                continue
            cleaned.append(element)
        page.elements = cleaned
    return pages


def build_document_plan(
    source: Path,
    packets: list[PagePacket],
    model: str,
    checkpoint_path: Path,
    workers: int = 2,
) -> DocumentPlan:
    existing: dict[int, PagePlan] = {}
    if checkpoint_path.exists():
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        existing = {p["page_number"]: PagePlan.model_validate(p) for p in data.get("pages", [])}

    pending = [packet for packet in packets if packet.page_number not in existing]
    def save_checkpoint() -> None:
        ordered = [existing[key] for key in sorted(existing)]
        payload = {
            "source_file": source.name,
            "title": infer_title(source, ordered),
            "language": "en-US",
            "pages": [page.model_dump(mode="json") for page in ordered],
        }
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if pending:
        client = OpenAI()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(plan_page, client, model, packet): packet for packet in pending}
            for future in as_completed(futures):
                packet = futures[future]
                plan = future.result()
                existing[packet.page_number] = plan
                save_checkpoint()
                print(f"planned page {packet.page_number}/{len(packets)}")

    pages = normalize_document_pages(source, [existing[key] for key in sorted(existing)])
    return DocumentPlan(
        source_file=source.name,
        title=infer_title(source, pages),
        language="en-US",
        pages=pages,
    )
