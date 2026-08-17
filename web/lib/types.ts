/**
 * The NDJSON frame contract, mirroring `src/rag_api/telemetry.py` and
 * `rag_core.contracts.Telemetry.as_dict()`.
 *
 * Frames arrive in this order:
 *
 *     meta     after retrieval — the gate decision and retrieval latency
 *     token*   the answer, or the server-authored decline copy
 *     sources  immediately BEFORE the first token, and only once both gates clear
 *     done     timings, token count, and which provider served
 *
 * Two properties of that order are load-bearing and easy to get wrong:
 *
 * `sources` precedes the first token rather than following the answer, so a
 * consumer that waits for `done` to attach citations renders an answer whose
 * markers are dead for its entire duration.
 *
 * `meta` may never arrive at all. When retrieval itself fails the server emits
 * `error` then `done` and nothing else, so nothing may assume telemetry exists
 * before rendering.
 */

export interface Source {
  chunk_id: string;
  title: string;
  /** The page anchor a citation resolves to. */
  page: number;
  snippet: string;
}

export interface GateTelemetry {
  proceed: boolean;
  reason: string;
  /**
   * The two conditions, separately, all the way to the pixel.
   *
   * ADR-003 chose a two-condition gate precisely so telemetry could say WHICH
   * one failed — "worth the second parameter on its own". Collapsing them into
   * a single confidence number anywhere, including in the interface, throws
   * away the entire reason the gate has two parameters.
   */
  similarity_ok: boolean;
  lexical_support: boolean;
  /** Null when the gate produced a non-finite value; `_jsonable` converts it. */
  top_similarity: number | null;
}

/** Everything known once retrieval has returned. */
export interface MetaTelemetry {
  gate: GateTelemetry;
  latency: { retrieval_ms: number | null };
  fused_scores: (number | null)[];
}

/** Everything only knowable once the stream has ended. */
export interface DoneTelemetry {
  latency: { ttft_ms: number | null };
  total_tokens: number;
  /** Which provider actually served — null when none did. */
  provider: string | null;
  truncated: boolean;
}

export type Frame =
  | { type: "meta"; telemetry: MetaTelemetry }
  | { type: "token"; text: string }
  | { type: "sources"; items: Source[] }
  | {
      type: "done";
      telemetry: DoneTelemetry;
      was_declined: boolean;
      decline_reason: string | null;
    }
  | { type: "error"; code: string; message: string };

/** The strip's view: the two halves merged, either of which may be missing. */
export interface Telemetry {
  meta: MetaTelemetry | null;
  done: DoneTelemetry | null;
}
