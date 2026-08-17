"""Assemble the corpus from committed fixture text.

Three FDA drug labels, public domain, pinned by `set_id` in `manifest.json`.
The corpus is fixed and ingested offline, so there is no upload path and no
document parsing — the text is right here.

WHY THERE IS NO PDF STEP
------------------------
The local build renders these same fixtures into a PDF and reads them back with
pypdf, which "exercises the page-aware chunker the way an uploaded document
would". That round-trip is lossy, and measurably so: its PDF writer escapes the
characters that would corrupt the *file* (`\\`, `(`, `)`) but emits the text as
raw UTF-8 into a content stream declared `/Helvetica` with no encoding, so pypdf
decodes those bytes as Latin-1.

Measured across all three labels: every one of the 28 non-ASCII characters
corrupts.

    β-lactamase  ->  Î²-lactamase        (7 of 13 amoxicillin pages)
    patient's    ->  patientâ€™s          (2 of 9 metformin pages)
    Program's    ->  Programâ€™s          (4 of 15 atenolol pages)

So the local build embedded `Î²-lactamase`, indexed it, and a visitor asking
about β-lactamase gets no lexical match at all. This profile ingests the text
directly instead: lossless, and one dependency lighter.

The consequence for TICKET-9 is that the local retrieval baseline was measured
over corrupted text, so a hosted-vs-local comparison has a third variable in it
alongside pgvector and ts_rank_cd. That belongs in the write-up, not in a
silently better number.

Pagination survives the PDF's removal because the page number is the citation
anchor: chunks never span a page boundary, so every chunk resolves to an exact
page (ARCHITECTURE.md §6).
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

from rag_core.chunking import PageText

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CHARS_PER_PAGE = 1200

# Order is load-bearing. It fixes the order sections appear in, which fixes
# pagination, which fixes page numbers, which fixes what a citation points at.
# Do not sort this, and do not fall back to the JSON's key order.
SECTION_TITLES = {
    "indications_and_usage": "INDICATIONS AND USAGE",
    "dosage_and_administration": "DOSAGE AND ADMINISTRATION",
    "contraindications": "CONTRAINDICATIONS",
    "adverse_reactions": "ADVERSE REACTIONS",
    "drug_interactions": "DRUG INTERACTIONS",
}


def _paginate(text: str, chars_per_page: int = CHARS_PER_PAGE) -> list[str]:
    """Split into pages on word boundaries, never mid-word.

    Normalises runs of whitespace to single spaces as a side effect. That is
    existing behaviour and the manifest's `verified_absent` claims were computed
    against it, so leave it alone.
    """
    words, pages, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > chars_per_page:
            pages.append(current.strip())
            current = ""
        current += word + " "
    if current.strip():
        pages.append(current.strip())
    return pages


def assemble_text(included: dict[str, str]) -> str:
    """One normalised string over the included sections, in a stable order."""
    parts = []
    for key, title in SECTION_TITLES.items():
        body = included.get(key)
        if body:
            parts.append(f"{title}\n{re.sub(r'\\s+', ' ', body).strip()}")
    return "\n\n".join(parts)


def corpus_text(title: str, included: dict[str, str]) -> str:
    """The exact string this document contains. Nothing transforms it after
    this point — which is the property the PDF round-trip used to break."""
    return f"{title}\n\n{assemble_text(included)}"


def load_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return manifest


def load_drug(slug: str) -> dict[str, Any]:
    drug: dict[str, Any] = json.loads((FIXTURES / f"{slug}.json").read_text(encoding="utf-8"))
    return drug


def document_title(slug: str) -> str:
    """Display title, used for citations. The fixtures carry no title of their
    own, so it is derived from the slug as the local build does."""
    return slug.title()


def paginate_document(slug: str) -> list[PageText]:
    """The document as pages, 1-based, ready for `chunk_pages`."""
    included = load_drug(slug)["included"]
    pages = _paginate(corpus_text(document_title(slug), included))
    return [PageText(page_number=i, text=text) for i, text in enumerate(pages, start=1)]
