import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import TelemetryStrip from "./TelemetryStrip";
import type { DoneTelemetry, MetaTelemetry } from "@/lib/types";

afterEach(cleanup);

const META: MetaTelemetry = {
  gate: {
    proceed: true,
    reason: "proceed",
    similarity_ok: true,
    lexical_support: true,
    top_similarity: 0.7123,
  },
  latency: { retrieval_ms: 41.6 },
  fused_scores: [0.0328, 0.0159],
};

const DONE: DoneTelemetry = {
  latency: { ttft_ms: 618.4 },
  total_tokens: 34,
  provider: "llama-3.3-70b-versatile",
  truncated: false,
};

const refused = (over: Partial<MetaTelemetry["gate"]>): MetaTelemetry => ({
  ...META,
  gate: { ...META.gate, proceed: false, ...over },
});

test("the two gate conditions are reported separately", () => {
  // AC #4, and the reason ADR-003 accepted a second tuning parameter at all.
  render(<TelemetryStrip meta={META} done={DONE} />);
  expect(screen.getByText("similarity")).toBeDefined();
  expect(screen.getByText("lexical support")).toBeDefined();
});

/** The label -> value pairs currently on screen. */
function strip(): Record<string, string> {
  const definitions = screen.getAllByRole("definition");
  return Object.fromEntries(
    screen
      .getAllByRole("term")
      .map((term, i) => [term.textContent ?? "", definitions[i]?.textContent ?? ""]),
  );
}

test("an off-domain refusal names similarity as the condition that decided it", () => {
  // AC #4. `off_domain` means top similarity fell below tau_abstain, which is
  // decisive whatever the lexical leg said.
  render(
    <TelemetryStrip
      meta={refused({ reason: "off_domain", similarity_ok: false, lexical_support: false })}
      done={null}
    />,
  );
  expect(strip()["similarity"]).toContain("not met");
  expect(strip()["similarity"]).toContain("decided this");
  expect(strip()["lexical support"]).not.toContain("decided this");
});

test("a weak_unsupported refusal names lexical support as the condition that decided it", () => {
  // The middle band, and the whole reason ADR-003 accepted a second parameter:
  // "metformin dosing" and "metformin pediatric dosing" sit at nearly
  // identical cosine distance, and only the lexical leg separates them.
  render(
    <TelemetryStrip
      meta={refused({ reason: "weak_unsupported", similarity_ok: true, lexical_support: false })}
      done={null}
    />,
  );
  expect(strip()["lexical support"]).toContain("not met");
  expect(strip()["lexical support"]).toContain("decided this");
  expect(strip()["similarity"]).toBe("met");
});

test("a passing gate with no lexical agreement does not report a failed condition", () => {
  // Found by running the real thing: the gate only consults lexical support in
  // the middle band (rag_core/gate.py), so above tau_strong it answers with
  // lexical_support false. Rendering that as a bare "not met" beside
  // "answered" reads as the system overriding its own gate. It did not — the
  // condition was never consulted.
  render(
    <TelemetryStrip
      meta={{ ...META, gate: { ...META.gate, proceed: true, lexical_support: false } }}
      done={DONE}
    />,
  );
  expect(strip()["lexical support"]).toBe("not required here");
  expect(document.body.textContent).toContain("Similarity alone was high enough");
});

test("both conditions holding is stated plainly", () => {
  render(<TelemetryStrip meta={META} done={DONE} />);
  expect(strip()["similarity"]).toBe("met");
  expect(strip()["lexical support"]).toBe("met");
  expect(document.body.textContent).toContain("Both conditions held");
});

test("each refusal reason gets an explanation rather than a bare code", () => {
  // The strip is a product feature, not debug output: "weak_unsupported" alone
  // means nothing to a reader.
  for (const reason of ["off_domain", "weak_unsupported", "empty_corpus"]) {
    cleanup();
    render(<TelemetryStrip meta={refused({ reason })} done={null} />);
    const body = document.body.textContent ?? "";
    expect(body.length).toBeGreaterThan(reason.length + 100);
    expect(body).toContain(reason);
  }
});

test("the failing condition is not merged into one confidence number", () => {
  // The merge ADR-003 rejected. Doing it here would undo the decision in the
  // one place nobody would look for it.
  render(
    <TelemetryStrip
      meta={refused({ reason: "off_domain", similarity_ok: false, lexical_support: false })}
      done={null}
    />,
  );
  const body = document.body.textContent ?? "";
  expect(body).toContain("similarity");
  expect(body).toContain("lexical support");
  expect(body.toLowerCase()).not.toContain("confidence score");
});

test("the gate decision and its reason are shown", () => {
  render(<TelemetryStrip meta={refused({ reason: "off_domain" })} done={null} />);
  expect(document.body.textContent).toContain("off_domain");
});

test("a meta-only strip renders — the refusal is explained before the text arrives", () => {
  // The whole point of splitting telemetry across two frames.
  render(<TelemetryStrip meta={META} done={null} />);
  expect(document.body.textContent).toContain("Telemetry");
  expect(document.body.textContent).toContain("0.712");
});

test("a done-only strip renders when meta never arrived", () => {
  // The retrieval-failure path emits error then done and no meta at all.
  render(<TelemetryStrip meta={null} done={{ ...DONE, provider: null, truncated: true }} />);
  expect(document.body.textContent).toContain("no gate decision");
});

test("nothing at all renders nothing, rather than an empty frame", () => {
  const { container } = render(<TelemetryStrip meta={null} done={null} />);
  expect(container.innerHTML).toBe("");
});

test("null numbers render as a dash, never as null or NaN", () => {
  // `_jsonable` converts non-finite floats to null on the way out, so these
  // arrive as genuine nulls and must not be coerced to a misleading zero.
  render(
    <TelemetryStrip
      meta={{
        gate: { ...META.gate, top_similarity: null },
        latency: { retrieval_ms: null },
        fused_scores: [],
      }}
      done={{ latency: { ttft_ms: null }, total_tokens: 0, provider: null, truncated: false }}
    />,
  );
  const body = document.body.textContent ?? "";
  expect(body).not.toContain("NaN");
  expect(body).not.toContain("null");
  expect(body).toContain("—");
});

test("a zero-token answer reports zero rather than a dash", () => {
  // Zero is a measurement; absent is not. Conflating them would hide a real
  // empty response behind the same glyph as missing telemetry.
  render(<TelemetryStrip meta={META} done={{ ...DONE, total_tokens: 0 }} />);
  const terms = screen.getAllByRole("term");
  const definitions = screen.getAllByRole("definition");
  const tokens = terms.findIndex((t) => t.textContent === "tokens");
  expect(definitions[tokens].textContent).toBe("0");
});

test("the serving provider is named", () => {
  // PRD F14 / ADR-004: which half of the failover chain answered.
  render(<TelemetryStrip meta={META} done={DONE} />);
  expect(document.body.textContent).toContain("llama-3.3-70b-versatile");
});

test("both latencies are reported, and separately", () => {
  // Retrieval time and time-to-first-token measure different things; one
  // number for both would hide which half was slow.
  render(<TelemetryStrip meta={META} done={DONE} />);
  const body = document.body.textContent ?? "";
  expect(body).toContain("42 ms");
  expect(body).toContain("618 ms");
});

test("fused scores are labelled so their near-constancy reads as intentional", () => {
  render(<TelemetryStrip meta={META} done={DONE} />);
  const cell = screen.getByText("fused scores").closest("div");
  expect(cell?.getAttribute("title")).toContain("rank");
});

test("a truncated stream is called out", () => {
  render(<TelemetryStrip meta={META} done={{ ...DONE, truncated: true }} />);
  expect(document.body.textContent).toContain("incomplete");
});
