"use client";

import { Fragment } from "react";

const MARKER = /\[(\d+)\]/g;

/**
 * Render answer text with its [n] citation markers as buttons.
 *
 * The model is instructed to cite the numbered context chunks, and those
 * numbers are assignable: `format_context` (rag_core/prompts.py) numbers
 * chunks from 1 in the same order `_sources_payload` (rag_api/streaming.py)
 * serialises them, so `[n]` is always `sources[n-1]`. That mapping is the only
 * reason this component can exist — without it the markers would be
 * decoration. It is also invisible from here: nothing in the frontend would
 * break loudly if either side reordered, so this comment is the guard.
 *
 * A marker pointing past the end of `sources` renders as plain text rather
 * than a dead button. Small models do occasionally invent a citation number,
 * and an affordance that does nothing is worse than no affordance. It is also
 * logged — ARCHITECTURE.md §7 calls an unresolvable citation a bug and says it
 * is "logged as one" — but not shown, because it is a model quirk a reader
 * cannot act on.
 */
export default function AnswerText({
  text,
  sourceCount,
  activeIndex,
  onCite,
}: {
  text: string;
  sourceCount: number;
  activeIndex: number | null;
  onCite: (index: number) => void;
}) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let key = 0;

  for (const match of text.matchAll(MARKER)) {
    const start = match.index ?? 0;
    const index = Number(match[1]) - 1;

    if (start > cursor) parts.push(<Fragment key={key++}>{text.slice(cursor, start)}</Fragment>);

    if (index >= 0 && index < sourceCount) {
      const active = index === activeIndex;
      parts.push(
        <button
          key={key++}
          type="button"
          onClick={() => onCite(index)}
          aria-label={`Show source ${index + 1}`}
          className={`mx-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded px-1 align-baseline text-[11px] font-semibold tabular-nums transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1
            ${
              active
                ? "bg-primary text-primary-foreground"
                : "bg-accent text-accent-foreground hover:bg-primary hover:text-primary-foreground"
            }`}
        >
          {index + 1}
        </button>,
      );
    } else {
      console.error(
        `unresolvable citation ${match[0]}: the answer cites a source that was not `
          + `returned (${sourceCount} available). The [n] -> sources[n-1] mapping `
          + `depends on format_context and _sources_payload agreeing on order.`,
      );
      parts.push(<Fragment key={key++}>{match[0]}</Fragment>);
    }
    cursor = start + match[0].length;
  }

  if (cursor < text.length) parts.push(<Fragment key={key++}>{text.slice(cursor)}</Fragment>);

  return <p className="whitespace-pre-wrap leading-7">{parts}</p>;
}
