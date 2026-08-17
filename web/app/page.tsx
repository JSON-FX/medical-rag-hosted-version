"use client";

import { useEffect, useReducer, useRef } from "react";

import Disclaimer from "@/components/Disclaimer";
import ExampleQuestions from "@/components/ExampleQuestions";
import QuestionBox from "@/components/QuestionBox";
import TurnView from "@/components/TurnView";
import { streamChat } from "@/lib/api";
import { chatReducer, initialChatState, isTerminalFrame } from "@/lib/chatReducer";
import { PRODUCT_NAME, STREAM_CUT_OFF, TAGLINE } from "@/lib/copy";

export default function Home() {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const inFlight = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  // A visitor who navigates away mid-answer would otherwise leave the request
  // open. On a serverless deployment that holds a function for its full
  // duration budget, which is a real cost rather than a tidiness point.
  useEffect(() => () => inFlight.current?.abort(), []);

  async function ask(question: string) {
    if (state.streaming) return;

    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    dispatch({ type: "ask", question });

    let ended = false;
    for await (const frame of streamChat({ question, signal: controller.signal })) {
      if (isTerminalFrame(frame)) ended = true;
      dispatch({ type: "frame", frame });
      bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }

    // A body truncated by the server dying is byte-identical to a clean end of
    // stream at the fetch layer. Without this the question box stays disabled
    // forever, waiting for a frame that is never coming.
    if (!ended && !controller.signal.aborted) {
      dispatch({ type: "failed", message: STREAM_CUT_OFF });
    }
  }

  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-6 sm:py-12">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">{PRODUCT_NAME}</h1>
        <p className="mt-2 max-w-prose text-[15px] text-muted-foreground">{TAGLINE}</p>
      </header>

      <div className="mt-4">
        <Disclaimer />
      </div>

      <div className="mt-6">
        <QuestionBox onAsk={ask} busy={state.streaming} />
        <ExampleQuestions onAsk={ask} busy={state.streaming} />
      </div>

      {state.turns.length > 0 ? (
        <section className="mt-10 flex flex-col gap-8" aria-label="Transcript">
          {state.turns.map((turn, index) =>
            turn.role === "user" ? (
              <article key={index} className="border-t border-border pt-6 first:border-0 first:pt-0">
                <TurnView turn={turn} index={index} />
                {state.turns[index + 1]?.role === "assistant" ? (
                  <TurnView turn={state.turns[index + 1]} index={index + 1} />
                ) : null}
              </article>
            ) : null,
          )}
        </section>
      ) : null}

      <div ref={bottom} />

      <footer className="mt-16 border-t border-border pt-4 text-xs text-muted-foreground">
        <p>
          Corpus: three public FDA drug labels (metformin, atenolol, amoxicillin). Retrieval is
          dense + lexical, fused by reciprocal rank; a two-condition gate decides whether to
          answer at all. The telemetry under each answer is that decision, not a debug panel.
        </p>
      </footer>
    </main>
  );
}
