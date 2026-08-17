"use client";

import { EXAMPLES, EXAMPLES_HEADING, EXAMPLES_NOTE } from "@/lib/copy";

/**
 * How the refusal gets found.
 *
 * PRD G5 is that a refusal is discoverable in under a minute by someone who
 * was *not told about it*. A labelled example is the mechanism, and the label
 * has to be honest — the refusing examples are drawn from the axes
 * `manifest.json` records as measured absent from the corpus, not from a guess
 * about what it probably lacks. See `lib/copy.ts`.
 */
export default function ExampleQuestions({
  onAsk,
  busy,
}: {
  onAsk: (question: string) => void;
  busy: boolean;
}) {
  return (
    <section className="mt-4" aria-labelledby="examples-heading">
      <h2 id="examples-heading" className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {EXAMPLES_HEADING}
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">{EXAMPLES_NOTE}</p>
      <ul className="mt-2.5 grid gap-2 sm:grid-cols-2">
        {EXAMPLES.map((example) => (
          <li key={example.question}>
            <button
              type="button"
              onClick={() => onAsk(example.question)}
              disabled={busy}
              className="w-full rounded-lg border border-border bg-card px-3.5 py-2.5 text-left transition-colors hover:border-primary/50 hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span className="block text-sm">{example.question}</span>
              <span
                className={`mt-1 block text-xs ${
                  example.refuses ? "text-warn" : "text-muted-foreground"
                }`}
              >
                {example.note}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
