"use client";

import { useState } from "react";

import AnswerText from "./AnswerText";
import SourceList from "./SourceList";
import TelemetryStrip from "./TelemetryStrip";
import type { Turn } from "@/lib/chatReducer";
import { DECLINE_RATIONALE, declineLabel, errorLabel, TRUNCATED_NOTE } from "@/lib/copy";

/**
 * One question and what came back.
 *
 * The three assistant outcomes are styled as three different things on
 * purpose. An answer, a refusal and a failure are not degrees of the same
 * result: PRD success criterion 2 is that a refusal reads as *deliberate*, and
 * a refusal rendered in the same grey box as an error reads as a failure no
 * matter how carefully the backend decided it.
 */
export default function TurnView({ turn, index }: { turn: Turn; index: number }) {
  const [activeSource, setActiveSource] = useState<number | null>(null);
  const turnId = `turn-${index}`;

  if (turn.role === "user") {
    return (
      <p className="text-base font-medium text-foreground sm:text-lg">{turn.text}</p>
    );
  }

  return (
    <div className="mt-3">
      {/*
        AC #5. A streaming answer is invisible to a screen reader without this.
        `polite` rather than `assertive`: an assertive region interrupts on
        every token, which turns a streaming answer into an unusable stutter.
      */}
      <div aria-live="polite" aria-busy={!turn.done} aria-atomic="false">
        {turn.kind === "pending" ? (
          <p className="text-sm text-muted-foreground">
            Searching the labels
            <span className="streaming-caret ml-0.5" aria-hidden="true">
              ▍
            </span>
          </p>
        ) : null}

        {turn.kind === "answer" ? (
          <div className="text-[15px]">
            <AnswerText
              text={turn.text}
              sourceCount={turn.sources.length}
              activeIndex={activeSource}
              onCite={(citedIndex) => {
                setActiveSource(citedIndex);
                document.getElementById(`${turnId}-source-${citedIndex}`)?.focus();
              }}
            />
            {!turn.done ? (
              <span className="streaming-caret text-primary" aria-hidden="true">
                ▍
              </span>
            ) : null}
          </div>
        ) : null}

        {turn.kind === "decline" ? (
          <div className="rounded-lg border border-warn/35 bg-warn/8 p-4">
            <p className="text-xs font-semibold tracking-wide text-warn uppercase">
              {declineLabel(turn.declineReason)}
            </p>
            {/* The sentence itself is server-authored (rag_core/prompts.py) and
                rendered exactly as received, so that it is identical here and
                in the evaluation harness. */}
            <p className="mt-1.5 text-[15px] leading-7">{turn.text}</p>
            <p className="mt-2.5 text-sm text-muted-foreground">{DECLINE_RATIONALE}</p>
          </div>
        ) : null}

        {turn.kind === "error" ? (
          <div className="rounded-lg border border-destructive/35 bg-destructive/8 p-4">
            <p className="text-xs font-semibold tracking-wide text-destructive uppercase">
              {errorLabel(turn.errorCode)}
            </p>
            {/* Whatever streamed before the failure stays on screen: it is real
                output the reader watched arrive, and discarding it makes a
                cut-off answer indistinguishable from one that never started. */}
            {turn.text ? (
              <p className="mt-1.5 text-[15px] leading-7 opacity-80">{turn.text}</p>
            ) : null}
            <p className="mt-1.5 text-sm">{turn.errorMessage}</p>
          </div>
        ) : null}
      </div>

      {turn.truncated && turn.kind !== "error" ? (
        <p className="mt-2 text-sm text-warn">{TRUNCATED_NOTE}</p>
      ) : null}

      <SourceList sources={turn.sources} activeIndex={activeSource} turnId={turnId} />

      <TelemetryStrip meta={turn.meta} done={turn.doneTelemetry} />
    </div>
  );
}
