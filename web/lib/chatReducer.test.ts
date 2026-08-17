import { describe, expect, test } from "vitest";

import {
  chatReducer,
  initialChatState,
  isTerminalFrame,
  type ChatAction,
  type ChatState,
} from "./chatReducer";
import type { DoneTelemetry, Frame, MetaTelemetry, Source } from "./types";

const SOURCE: Source = {
  chunk_id: "metformin_0",
  title: "Metformin",
  page: 3,
  snippet: "Starting dose 500 mg",
};

const META: MetaTelemetry = {
  gate: {
    proceed: true,
    reason: "proceed",
    similarity_ok: true,
    lexical_support: true,
    top_similarity: 0.71,
  },
  latency: { retrieval_ms: 41.2 },
  fused_scores: [0.0328, 0.0159],
};

const DONE: DoneTelemetry = {
  latency: { ttft_ms: 620 },
  total_tokens: 34,
  provider: "llama-3.3-70b-versatile",
  truncated: false,
};

function run(actions: ChatAction[]): ChatState {
  return actions.reduce(chatReducer, initialChatState);
}

const ask = (question: string): ChatAction => ({ type: "ask", question });
const frame = (f: Frame): ChatAction => ({ type: "frame", frame: f });
type DoneFrame = Extract<Frame, { type: "done" }>;
const done = (over: Partial<DoneFrame> = {}): DoneFrame => ({
  type: "done",
  telemetry: DONE,
  was_declined: false,
  decline_reason: null,
  ...over,
});

describe("turn bookkeeping", () => {
  test("asking appends a user turn and a pending assistant turn", () => {
    const state = run([ask("What is the dose?")]);
    expect(state.turns).toHaveLength(2);
    expect(state.turns[0]).toMatchObject({ role: "user", text: "What is the dose?" });
    expect(state.turns[1]).toMatchObject({ role: "assistant", kind: "pending", done: false });
    expect(state.streaming).toBe(true);
  });

  test("a second question appends to the transcript rather than replacing it", () => {
    // D2: the transcript is what lets an evaluator put a grounded answer and a
    // refusal side by side, which is what makes the refusal read as deliberate.
    const state = run([
      ask("first"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "an answer" }),
      frame(done()),
      ask("second"),
    ]);
    expect(state.turns).toHaveLength(4);
    expect(state.turns[0].text).toBe("first");
    expect(state.turns[1].text).toBe("an answer");
    expect(state.turns[2].text).toBe("second");
  });
});

describe("answers", () => {
  test("a sources frame marks the turn an answer and attaches citations", () => {
    const state = run([
      ask("q"),
      frame({ type: "meta", telemetry: META }),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "500 mg" }),
    ]);
    const turn = state.turns[1];
    expect(turn.kind).toBe("answer");
    expect(turn.sources).toEqual([SOURCE]);
    expect(turn.text).toBe("500 mg");
  });

  test("citations are live before the answer finishes", () => {
    // `sources` arrives immediately BEFORE the first token, not after the
    // answer. A reducer that waited for `done` to attach them would render an
    // answer whose markers are dead for its entire duration.
    const state = run([
      ask("q"),
      frame({ type: "meta", telemetry: META }),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "The dose is 500 mg [1]" }),
    ]);
    expect(state.turns[1].sources).toHaveLength(1);
    expect(state.turns[1].done).toBe(false);
  });

  test("tokens accumulate in order", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "500 " }),
      frame({ type: "token", text: "mg" }),
    ]);
    expect(state.turns[1].text).toBe("500 mg");
  });

  test("done ends the turn and stops streaming", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "500 mg" }),
      frame(done()),
    ]);
    expect(state.turns[1]).toMatchObject({ kind: "answer", done: true, truncated: false });
    expect(state.streaming).toBe(false);
  });

  test("a truncated done marks the answer incomplete", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "partial" }),
      frame(done({ telemetry: { ...DONE, truncated: true } })),
    ]);
    expect(state.turns[1].truncated).toBe(true);
  });
});

describe("telemetry arrives in two halves", () => {
  test("meta populates the strip before any token", () => {
    const state = run([ask("q"), frame({ type: "meta", telemetry: META })]);
    expect(state.turns[1].meta).toEqual(META);
    expect(state.turns[1].doneTelemetry).toBeNull();
  });

  test("done merges rather than replacing meta", () => {
    // The two halves carry different fields — the gate and retrieval latency
    // in one, TTFT and the serving provider in the other. A reducer that
    // replaced would blank the gate at the moment the answer finished.
    const state = run([
      ask("q"),
      frame({ type: "meta", telemetry: META }),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "x" }),
      frame(done()),
    ]);
    expect(state.turns[1].meta).toEqual(META);
    expect(state.turns[1].doneTelemetry).toEqual(DONE);
  });

  test("a refusal has its full gate telemetry before the decline text", () => {
    // The whole reason TICKET-5 split the telemetry across two frames: the
    // reader sees WHY before WHAT.
    const refused: MetaTelemetry = {
      ...META,
      gate: {
        proceed: false,
        reason: "off_domain",
        similarity_ok: false,
        lexical_support: true,
        top_similarity: 0.21,
      },
    };
    const state = run([ask("q"), frame({ type: "meta", telemetry: refused })]);
    expect(state.turns[1].meta?.gate.proceed).toBe(false);
    expect(state.turns[1].text).toBe("");
  });

  test("meta never arriving leaves the turn renderable", () => {
    // The retrieval-failure path: the server emits `error` then `done` and no
    // `meta` at all. Nothing may assume telemetry exists before rendering.
    const state = run([
      ask("q"),
      frame({ type: "error", code: "provider_unavailable", message: "temporarily unavailable" }),
      frame(done({ telemetry: { ...DONE, truncated: true, provider: null, total_tokens: 0 } })),
    ]);
    expect(state.turns[1].meta).toBeNull();
    expect(state.turns[1].kind).toBe("error");
    expect(state.turns[1].doneTelemetry?.truncated).toBe(true);
  });

  test("null latencies and similarities survive intact", () => {
    // `_jsonable` converts non-finite floats to null, so the strip must be
    // handed the null rather than a coerced zero.
    const nulled: MetaTelemetry = {
      gate: { ...META.gate, top_similarity: null },
      latency: { retrieval_ms: null },
      fused_scores: [null],
    };
    const state = run([ask("q"), frame({ type: "meta", telemetry: nulled })]);
    expect(state.turns[1].meta?.gate.top_similarity).toBeNull();
    expect(state.turns[1].meta?.latency.retrieval_ms).toBeNull();
  });
});

describe("declines", () => {
  test("a token with no preceding sources is a decline", () => {
    // Sources arrive if and only if the turn is an answer. That invariant is
    // what lets a decline render as a decline from its first character instead
    // of restyling when done arrives.
    const state = run([
      ask("capital of France?"),
      frame({ type: "meta", telemetry: META }),
      frame({ type: "token", text: "I can only answer questions grounded in..." }),
    ]);
    expect(state.turns[1].kind).toBe("decline");
  });

  test("done supplies the decline reason", () => {
    const state = run([
      ask("q"),
      frame({ type: "token", text: "copy" }),
      frame(done({ was_declined: true, decline_reason: "off_domain" })),
    ]);
    expect(state.turns[1]).toMatchObject({
      kind: "decline",
      declineReason: "off_domain",
      done: true,
    });
  });

  test("a decline that arrives with no tokens is still a decline", () => {
    const state = run([
      ask("q"),
      frame(done({ was_declined: true, decline_reason: "weak_unsupported" })),
    ]);
    expect(state.turns[1].kind).toBe("decline");
  });

  test("server decline copy is preserved verbatim", () => {
    // It is server-authored on purpose (rag_core/prompts.py), which is what
    // makes it identical here and in the eval harness. The UI renders it; it
    // must never write its own.
    const copy =
      "I can't answer that from the three drug labels this system has indexed.";
    const state = run([ask("q"), frame({ type: "token", text: copy })]);
    expect(state.turns[1].text).toBe(copy);
  });

  test("a decline carries no sources", () => {
    const state = run([
      ask("q"),
      frame({ type: "meta", telemetry: META }),
      frame({ type: "token", text: "decline copy" }),
      frame(done({ was_declined: true, decline_reason: "off_domain" })),
    ]);
    expect(state.turns[1].sources).toEqual([]);
  });
});

describe("errors", () => {
  test("an error frame marks the turn an error and records the code", () => {
    const state = run([
      ask("q"),
      frame({ type: "error", code: "all_providers_unavailable", message: "both down" }),
    ]);
    expect(state.turns[1]).toMatchObject({
      kind: "error",
      errorCode: "all_providers_unavailable",
    });
  });

  test("the server's message is kept for display", () => {
    // errors.py writes these for a reader, not for a log.
    const message =
      "Both answer providers are unavailable right now. This demo runs on free tiers "
      + "with no availability guarantee — please try again shortly.";
    const state = run([
      ask("q"),
      frame({ type: "error", code: "all_providers_unavailable", message }),
    ]);
    expect(state.turns[1].errorMessage).toBe(message);
  });

  test("an error frame on its own ends the turn and stops streaming", () => {
    const state = run([
      ask("q"),
      frame({ type: "error", code: "provider_unavailable", message: "unavailable" }),
    ]);
    expect(state.turns[1].done).toBe(true);
    expect(state.streaming).toBe(false);
  });

  test("a transport failure with no frames at all is an error turn", () => {
    const state = run([ask("q"), { type: "failed", message: "Failed to fetch" }]);
    expect(state.turns[1]).toMatchObject({ kind: "error", done: true });
    expect(state.streaming).toBe(false);
  });

  test("an error frame is not reclassified as a decline by later tokens", () => {
    const state = run([
      ask("q"),
      frame({ type: "error", code: "provider_error", message: "rejected" }),
      frame({ type: "token", text: "stray" }),
    ]);
    expect(state.turns[1].kind).toBe("error");
  });

  test("the done frame that follows an error does not turn it into an answer", () => {
    // The server's error path emits error then done{was_declined:false}, so
    // this exact sequence happens whenever a provider dies. A turn that
    // flipped to "answer" here would render an empty answer instead of the
    // recovery message.
    const state = run([
      ask("q"),
      frame({ type: "error", code: "provider_unavailable", message: "refused" }),
      frame(done({ telemetry: { ...DONE, truncated: true } })),
    ]);
    expect(state.turns[1]).toMatchObject({ kind: "error", done: true });
    expect(state.streaming).toBe(false);
  });

  test("text that streamed before a mid-stream failure stays on screen", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "The adult starting dose is " }),
      frame({ type: "error", code: "provider_unavailable", message: "died" }),
      frame(done({ telemetry: { ...DONE, truncated: true } })),
    ]);
    expect(state.turns[1].text).toBe("The adult starting dose is ");
    expect(state.turns[1].sources).toEqual([SOURCE]);
    expect(state.turns[1].truncated).toBe(true);
  });
});

describe("a stream that ends without a done frame", () => {
  test("finishes the turn and re-enables the question box", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "Start with 500 mg" }),
      { type: "failed", message: "The connection dropped" },
    ]);
    expect(state.turns[1]).toMatchObject({ kind: "error", done: true });
    expect(state.streaming).toBe(false);
  });

  test("retains the text that already streamed", () => {
    const state = run([
      ask("q"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "Start with " }),
      frame({ type: "token", text: "500 mg" }),
      { type: "failed", message: "The connection dropped" },
    ]);
    expect(state.turns[1].text).toBe("Start with 500 mg");
    expect(state.turns[1].sources).toEqual([SOURCE]);
  });

  test("keeps the failure message for display", () => {
    const state = run([ask("q"), { type: "failed", message: "The connection dropped." }]);
    expect(state.turns[1].errorMessage).toBe("The connection dropped.");
  });

  test("isTerminalFrame agrees with the frames that set done", () => {
    expect(isTerminalFrame({ type: "token", text: "x" })).toBe(false);
    expect(isTerminalFrame({ type: "sources", items: [SOURCE] })).toBe(false);
    expect(isTerminalFrame({ type: "meta", telemetry: META })).toBe(false);
    expect(isTerminalFrame({ type: "error", code: "x", message: "y" })).toBe(true);
    expect(isTerminalFrame(done())).toBe(true);
  });
});

describe("purity", () => {
  test("the reducer does not mutate the state it is given", () => {
    const before = run([ask("q")]);
    const snapshot = JSON.stringify(before);
    chatReducer(before, frame({ type: "token", text: "x" }));
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  test("an earlier turn is untouched by a later one", () => {
    const first = run([
      ask("first"),
      frame({ type: "sources", items: [SOURCE] }),
      frame({ type: "token", text: "answered" }),
      frame(done()),
    ]);
    const second = [ask("second"), frame({ type: "token", text: "declined" })].reduce(
      chatReducer,
      first,
    );
    expect(second.turns[1].text).toBe("answered");
    expect(second.turns[1].kind).toBe("answer");
  });
});
