"""The corpus, as pure data. No database, no network."""

import re

import pytest

from ingest.axes import NEAR_MISS_AXES, verified_absent_axes
from ingest.corpus import (
    CHARS_PER_PAGE,
    SECTION_TITLES,
    assemble_text,
    corpus_text,
    document_title,
    load_drug,
    load_manifest,
    paginate_document,
)
from rag_core.chunking import chunk_pages
from rag_core.config import load_config

SLUGS = sorted(load_manifest()["drugs"])
CFG = load_config(env={})

# Sequences that appear when UTF-8 bytes are decoded as Latin-1. The local
# build's PDF round-trip produces all of these.
MOJIBAKE = re.compile(r"â€|[ÂÃÎ][\x80-\xbf]")


def test_the_corpus_is_the_three_pinned_labels():
    assert SLUGS == ["amoxicillin", "atenolol", "metformin"]


def test_every_drug_is_pinned_to_a_label_revision():
    """set_id names the exact openFDA revision. Without it, "the metformin
    label" is a moving target and the labelled question set drifts under it."""
    for slug, meta in load_manifest()["drugs"].items():
        assert meta["set_id"], slug


def test_the_source_is_recorded_as_public_domain():
    """PRD constraint: public corpus only."""
    assert "public domain" in load_manifest()["source"].lower()


# --- assembly ------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_sections_appear_in_the_declared_order(slug):
    """Section order fixes pagination, which fixes page numbers, which fixes
    what a citation points at."""
    text = assemble_text(load_drug(slug)["included"])
    positions = [text.find(title) for title in SECTION_TITLES.values() if title in text]
    assert positions == sorted(positions)
    assert positions, f"{slug} assembled to no known sections"


@pytest.mark.parametrize("slug", SLUGS)
def test_only_included_sections_are_assembled(slug):
    """A withheld section must not leak in, or the near-miss questions built on
    its absence become answerable."""
    meta = load_manifest()["drugs"][slug]
    included = load_drug(slug)["included"]
    assert set(included) <= set(meta["included_sections"])
    for withheld in meta["withheld_sections"]:
        assert withheld not in included


@pytest.mark.parametrize("slug", SLUGS)
def test_assembly_is_deterministic(slug):
    included = load_drug(slug)["included"]
    assert assemble_text(included) == assemble_text(included)


def test_the_document_title_leads_the_text():
    text = corpus_text("Metformin", load_drug("metformin")["included"])
    assert text.startswith("Metformin\n\n")


def test_the_display_title_is_derived_from_the_slug():
    assert document_title("metformin") == "Metformin"


# --- the mojibake regression guard ---------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_no_mojibake_survives_into_the_corpus(slug):
    """The reason this profile has no PDF round-trip.

    The local build renders these fixtures to a PDF and reads them back with
    pypdf, which decodes the raw UTF-8 content stream as Latin-1. All 28
    non-ASCII characters in the corpus corrupt: β-lactamase becomes
    Î²-lactamase, and a visitor searching the real term gets no lexical match.
    Ingesting the text directly avoids it entirely, and this pins that.
    """
    text = assemble_text(load_drug(slug)["included"])
    found = MOJIBAKE.findall(text)
    assert found == [], f"{slug} contains mojibake: {found[:5]}"


def test_the_greek_letters_that_corrupt_are_present_and_correct():
    """The specific characters the round-trip destroyed. If this ever fails,
    something has reintroduced a lossy encoding step."""
    text = assemble_text(load_drug("amoxicillin")["included"])
    assert "β-lactamase" in text
    assert "Î²" not in text


# --- pagination ----------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_pages_are_numbered_from_one_without_gaps(slug):
    pages = paginate_document(slug)
    assert [p.page_number for p in pages] == list(range(1, len(pages) + 1))


@pytest.mark.parametrize("slug", SLUGS)
def test_no_page_exceeds_the_page_size(slug):
    for page in paginate_document(slug):
        assert len(page.text) <= CHARS_PER_PAGE, f"{slug} p.{page.page_number}"


@pytest.mark.parametrize("slug", SLUGS)
def test_pagination_never_splits_a_word(slug):
    """Page boundaries fall between words, so a chunk's text is always readable
    and a citation never points at half a term."""
    pages = paginate_document(slug)
    rejoined = " ".join(p.text for p in pages)
    original = " ".join(corpus_text(document_title(slug), load_drug(slug)["included"]).split())
    assert rejoined == original


@pytest.mark.parametrize("slug", SLUGS)
def test_pagination_is_deterministic(slug):
    first = [(p.page_number, p.text) for p in paginate_document(slug)]
    second = [(p.page_number, p.text) for p in paginate_document(slug)]
    assert first == second


def test_the_page_counts_are_pinned():
    """Changing these means chunk ids move, which means every stored citation
    points somewhere new. It should be a deliberate act, not a surprise."""
    assert {slug: len(paginate_document(slug)) for slug in SLUGS} == {
        "amoxicillin": 13,
        "atenolol": 15,
        "metformin": 9,
    }


# --- chunking over the real corpus ---------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_every_chunk_belongs_to_exactly_one_page(slug):
    """The property that makes a page number a usable citation anchor: chunks
    never span a page boundary (ARCHITECTURE.md §6)."""
    pages = {p.page_number: p.text for p in paginate_document(slug)}
    for draft in chunk_pages(paginate_document(slug), CFG.chunk):
        assert draft.page_number in pages
        # Overlap prepends the tail of the previous chunk on the same page, so
        # the chunk is not always a literal substring — but its own new text is.
        assert draft.text.strip()


@pytest.mark.parametrize("slug", SLUGS)
def test_chunk_ordinals_are_contiguous_from_zero(slug):
    drafts = chunk_pages(paginate_document(slug), CFG.chunk)
    assert [d.chunk_index for d in drafts] == list(range(len(drafts)))


# --- the absence scan ----------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_the_manifests_absence_claims_hold(slug):
    """A near-miss question is only fair if the corpus genuinely lacks the
    answer, and withholding a SECTION does not guarantee that — metformin's
    dosage_and_administration discusses paediatric dosing even with
    pediatric_use withheld. TICKET-8's question set depends on this."""
    meta = load_manifest()["drugs"][slug]
    text = assemble_text(load_drug(slug)["included"])
    assert verified_absent_axes(text) == meta["verified_absent"]


@pytest.mark.parametrize("slug", SLUGS)
def test_every_claimed_absent_axis_is_a_known_axis(slug):
    meta = load_manifest()["drugs"][slug]
    assert set(meta["verified_absent"]) <= set(NEAR_MISS_AXES)


def test_the_absence_scan_uses_stems_not_literal_phrases():
    """Literal matching failed twice in the local build: "hepatic impairment"
    missed "history of liver disease". A false ABSENCE is the dangerous
    direction — it ships a near-miss that is actually answerable."""
    assert verified_absent_axes("history of liver disease") != []
    assert "hepatic" not in verified_absent_axes("history of liver disease")


def test_every_drug_has_at_least_one_absent_axis():
    """A drug with nothing verifiably absent contributes no near-miss questions,
    and the refusal path is what this demo exists to show."""
    for slug, meta in load_manifest()["drugs"].items():
        assert meta["verified_absent"], slug
