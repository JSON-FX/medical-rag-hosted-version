"use client";

import type { DoneTelemetry, GateTelemetry, MetaTelemetry } from "@/lib/types";

/**
 * What the pipeline decided, and how long each part of it took.
 *
 * ARCHITECTURE.md §3 calls this "a product feature, not debug output ... the
 * part that shows engineering rather than describing it". Six tickets of work
 * — a two-condition gate, a failover chain that reports who served, retrieval
 * latency measured separately from time-to-first-token — are invisible without
 * it.
 *
 * It fills in two stages, because the telemetry arrives in two frames: `meta`
 * after retrieval, `done` after the stream ends. Either can be missing. On a
 * refusal the gate half is complete BEFORE the decline text renders, which is
 * the entire reason TICKET-5 split it, and on a retrieval failure `meta` never
 * arrives at all.
 */
export default function TelemetryStrip({
  meta,
  done,
}: {
  meta: MetaTelemetry | null;
  done: DoneTelemetry | null;
}) {
  if (!meta && !done) return null;

  const gate = meta?.gate ?? null;

  return (
    <div className="mt-3 rounded-md border border-border bg-muted/50 px-3 py-2.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium tracking-wide text-muted-foreground uppercase">
          Telemetry
        </span>
        {gate ? (
          <span
            className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${
              gate.proceed ? "bg-ok/12 text-ok" : "bg-warn/15 text-warn"
            }`}
          >
            {gate.proceed ? "gate: answered" : `gate: refused (${gate.reason})`}
          </span>
        ) : null}
      </div>

      {gate ? (
        <>
          {/*
            The two conditions, side by side and never merged.

            ADR-003 chose a two-condition gate specifically so telemetry could
            say WHICH one failed — a high-similarity match with no lexical
            agreement is a different story from neither holding. Collapsing
            them into a single "confidence" number here would undo that
            decision at the last possible moment, in the one place nobody
            would think to look for it.
          */}
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
            <Condition
              label="similarity"
              value={gate.similarity_ok ? "met" : "not met"}
              decisive={decisiveCondition(gate) === "similarity"}
            />
            <Condition
              label="lexical support"
              value={lexicalVerdict(gate)}
              decisive={decisiveCondition(gate) === "lexical"}
            />
            <Metric label="top similarity" value={fixed(gate.top_similarity, 3)} />
            <Metric label="retrieval" value={ms(meta?.latency.retrieval_ms)} />
          </dl>
          <p className="mt-1.5 text-muted-foreground">{gateExplanation(gate)}</p>
        </>
      ) : (
        <p className="mt-2 text-muted-foreground">
          Retrieval did not complete, so there is no gate decision to report.
        </p>
      )}

      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 border-t border-border pt-2 sm:grid-cols-4">
        <Metric label="time to first token" value={ms(done?.latency.ttft_ms)} />
        <Metric label="tokens" value={done ? String(done.total_tokens) : null} />
        <Metric label="served by" value={done?.provider ?? null} mono />
        <Metric
          label="fused scores"
          value={
            meta?.fused_scores.length
              ? meta.fused_scores.map((s) => fixed(s, 4) ?? "—").join("  ")
              : null
          }
          /*
            These look nearly constant — 2/(60+1) whenever both retrievers agree
            on first place — and that is the point rather than a bug to hide.
            Shown beside the raw similarity above, it is ADR-003's argument made
            visible: RRF discards magnitude, so the gate reads the raw scores
            and never the fused one.
          */
          title="Reciprocal rank fusion (k=60). Near-constant when both retrievers agree on rank — which is why the gate reads raw similarity, not this."
        />
      </dl>

      {done?.truncated ? (
        <p className="mt-2 text-warn">Stream ended early; the answer above is incomplete.</p>
      ) : null}
    </div>
  );
}

/**
 * Which condition actually decided a refusal.
 *
 * The gate does not require both (rag_core/gate.py): below `tau_abstain` the
 * question is off-domain whatever the lexical leg says, and lexical support is
 * only consulted in the middle band between `tau_abstain` and `tau_strong`.
 * Above `tau_strong`, similarity carries the decision alone.
 *
 * The reason string is what names the cause, so it is read rather than
 * re-derived — the frontend does not receive the thresholds and guessing at
 * them would put a second copy of the gate's logic here, which is exactly what
 * ADR-003's "gate on raw scores in one place" is meant to prevent.
 */
function decisiveCondition(gate: GateTelemetry): "similarity" | "lexical" | null {
  if (gate.proceed) return null;
  if (gate.reason === "off_domain") return "similarity";
  if (gate.reason === "weak_unsupported") return "lexical";
  return null;
}

/**
 * "not met" is misleading when the gate answered anyway.
 *
 * A high-similarity match does not need lexical agreement, so reporting a bare
 * "not met" next to "answered" reads as a contradiction — as though the system
 * overrode its own gate. It did not: the condition simply was not consulted.
 */
function lexicalVerdict(gate: GateTelemetry): string {
  if (gate.lexical_support) return "met";
  return gate.proceed ? "not required here" : "not met";
}

function gateExplanation(gate: GateTelemetry): string {
  if (gate.proceed) {
    return gate.lexical_support
      ? "Both conditions held: the retrieved text is close to the question and shares its terms."
      : "Similarity alone was high enough to answer, so lexical agreement was not required.";
  }
  if (gate.reason === "off_domain") {
    return "Nothing retrieved was close enough to the question, so no model was called.";
  }
  if (gate.reason === "weak_unsupported") {
    return (
      "Close enough to be plausible, but the question's own terms do not appear in the "
      + "retrieved text — the case a similarity threshold alone would answer wrongly."
    );
  }
  if (gate.reason === "empty_corpus") {
    return "There is no index to search; this is a deployment fault, not a limit of the question.";
  }
  return "The confidence gate declined to answer.";
}

/** One half of the gate. Colour is reinforced by a word, never carried by it alone. */
function Condition({
  label,
  value,
  decisive,
}: {
  label: string;
  value: string;
  decisive: boolean;
}) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`font-medium ${decisive ? "text-warn" : ""}`}>
        {value}
        {decisive ? <span className="ml-1 text-[10px] font-normal">← decided this</span> : null}
      </dd>
    </div>
  );
}

function Metric({
  label,
  value,
  mono,
  title,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div title={title}>
      <dt className="text-muted-foreground">{label}</dt>
      {/* A dash, never `null` or `NaN`: every number here can legitimately be
          absent, because `_jsonable` converts non-finite floats to null and
          the two telemetry halves arrive at different times. */}
      <dd className={`font-medium tabular-nums ${mono ? "font-mono text-[11px]" : ""}`}>
        {value ?? "—"}
      </dd>
    </div>
  );
}

function fixed(value: number | null | undefined, places: number): string | null {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(places) : null;
}

function ms(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value)} ms` : null;
}
