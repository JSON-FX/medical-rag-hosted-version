"use client";

import { useRef, useState } from "react";

/**
 * The one control on the page.
 *
 * PRD success criterion 1 is that a stranger with the URL reaches a cited
 * answer *without instructions*, so this autofocuses: the first thing that
 * happens on load is that typing works.
 */
export default function QuestionBox({
  onAsk,
  busy,
}: {
  onAsk: (question: string) => void;
  busy: boolean;
}) {
  const [value, setValue] = useState("");
  const input = useRef<HTMLInputElement>(null);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const question = value.trim();
    if (!question || busy) return;
    setValue("");
    onAsk(question);
  }

  return (
    <form onSubmit={submit} className="flex gap-2">
      <label htmlFor="question" className="sr-only">
        Ask a question about metformin, atenolol or amoxicillin
      </label>
      <input
        id="question"
        ref={input}
        // Autofocus is normally a smell — it steals focus from whatever a
        // reader was doing. Here the page has one text input and one purpose,
        // and PRD criterion 1 is reaching an answer without instructions.
        autoFocus
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="Ask about metformin, atenolol or amoxicillin…"
        autoComplete="off"
        enterKeyHint="send"
        className="min-w-0 flex-1 rounded-lg border border-border bg-card px-3.5 py-2.5 text-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <button
        type="submit"
        // Disabled rather than cancelling, so two streams can never interleave
        // tokens into one turn. The in-flight request is also aborted by the
        // page when a new one starts, which covers the example buttons.
        disabled={busy || !value.trim()}
        className="rounded-lg bg-primary px-4 py-2.5 text-base font-medium text-primary-foreground transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45"
      >
        {busy ? "Asking…" : "Ask"}
      </button>
    </form>
  );
}
