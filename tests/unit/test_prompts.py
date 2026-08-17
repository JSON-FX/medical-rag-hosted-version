import pytest

from rag_core.gate import REASONS
from rag_core.prompts import (
    DECLINE_COPY,
    FALLBACK_DECLINE,
    SENTINEL,
    SYSTEM_TEMPLATE,
    ContextChunk,
    build_messages,
    decline_text,
)


def _chunks(n=2):
    return [
        ContextChunk(chunk_id=f"1_{i}", title="Monograph", page_number=i + 1, text=f"body {i}")
        for i in range(n)
    ]


def test_sentinel_is_the_exact_documented_string():
    assert SENTINEL == "INSUFFICIENT_CONTEXT"


def test_every_declining_gate_reason_has_copy():
    for reason in REASONS:
        if reason == "ok":
            continue
        assert reason in DECLINE_COPY
    assert "insufficient_context" in DECLINE_COPY


def test_decline_copy_is_distinct_per_reason():
    values = list(DECLINE_COPY.values())
    assert len(values) == len(set(values)), "each reason needs distinguishable copy"


def test_decline_text_falls_back_without_raising():
    assert decline_text("some_unknown_reason")


def test_empty_corpus_copy_names_it_as_a_deployment_fault():
    """Diverges from the local build, which invited the reader to upload a
    document. The corpus here is fixed and ingested offline, so an empty index
    is our fault and there is nothing the reader can do about it. Saying so is
    what keeps the refusal path reading as deliberate (PRD G5)."""
    copy = DECLINE_COPY["empty_corpus"].lower()
    assert "empty" in copy
    assert "deployment" in copy


def test_no_decline_copy_offers_a_remedy_the_reader_cannot_take():
    """Every string in the local build's copy suggested uploading a document.
    There is no upload in this profile (PRD §4, non-goals), and offering one
    makes a deliberate refusal read as a broken one."""
    for reason, copy in DECLINE_COPY.items():
        assert "upload" not in copy.lower(), f"{reason} still offers an upload"
    assert "upload" not in FALLBACK_DECLINE.lower()
    assert "upload" not in SYSTEM_TEMPLATE.lower()


def test_insufficient_context_copy_blames_the_source_not_the_user():
    copy = DECLINE_COPY["insufficient_context"].lower()
    assert "doesn't contain" in copy or "don't contain" in copy or "not contain" in copy


def test_system_message_instructs_the_sentinel():
    messages = build_messages("q", _chunks(), history=[])
    assert messages[0]["role"] == "system"
    assert SENTINEL in messages[0]["content"]


def test_context_chunks_are_numbered_with_title_and_page():
    system = build_messages("q", _chunks(2), history=[])[0]["content"]
    assert "[1]" in system and "[2]" in system
    assert "Monograph" in system
    assert "p. 1" in system


def test_question_is_the_final_user_message():
    messages = build_messages("what is the dose?", _chunks(), history=[])
    assert messages[-1] == {"role": "user", "content": "what is the dose?"}


def test_history_is_included_between_system_and_question():
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    messages = build_messages("now", _chunks(), history=history)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "earlier"


def test_history_is_capped_by_message_count():
    history = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    messages = build_messages("now", _chunks(), history=history, max_history=4)
    assert len(messages) == 1 + 4 + 1
    assert messages[1]["content"] == "m6"      # keeps the most recent 4


def test_history_is_also_capped_by_character_budget():
    """Two long turns must not crowd out retrieved context in an 8B window."""
    history = [{"role": "user", "content": "x" * 5000} for _ in range(4)]
    messages = build_messages("now", _chunks(), history=history, max_history=4, history_chars=1000)
    body = "".join(m["content"] for m in messages[1:-1])
    assert len(body) <= 1000


def test_system_prompt_carries_the_medical_disclaimer():
    system = build_messages("q", _chunks(), history=[])[0]["content"]
    assert "not a substitute" in system.lower()


def test_an_oversized_turn_does_not_discard_smaller_older_turns():
    """One pasted lab report must not evict every other turn behind it."""
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "short reply"},
    ]
    messages = build_messages("now", _chunks(), history, max_history=4, history_chars=200)
    kept = [m["content"] for m in messages[1:-1]]
    assert "earlier question" in kept, "a small older turn was dropped behind a large one"
    assert "x" * 5000 not in kept


def test_history_never_starts_with_an_orphaned_assistant_turn():
    """[system, assistant, user] gives the model a reply to a question it
    cannot see."""
    history = [
        {"role": "user", "content": "y" * 5000},
        {"role": "assistant", "content": "short reply"},
    ]
    messages = build_messages("now", _chunks(), history, max_history=4, history_chars=200)
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert roles[1] != "assistant", f"orphaned assistant turn: {roles}"


def test_sentinel_instruction_forbids_any_surrounding_text():
    """Detection is startswith-based; a 'Sure! ' preamble would defeat it."""
    system = build_messages("q", _chunks(), history=[])[0]["content"]
    lowered = system.lower()
    assert "nothing before it" in lowered
    assert "no greeting" in lowered


def test_build_messages_refuses_to_build_a_prompt_with_no_context():
    """Answering ungrounded is the failure mode this system exists to prevent."""
    with pytest.raises(ValueError, match="at least one context chunk"):
        build_messages("what is the dose?", [], history=[])
