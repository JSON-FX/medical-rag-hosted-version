"use client";

import type { Source } from "@/lib/types";

/**
 * The other half of a citation.
 *
 * AC #2 is that a citation *resolves* — clicking `[1]` has to land somewhere
 * that names a document and a page. A list nobody can reach from the text is
 * not resolution, so each entry is addressable by index and highlights when
 * its marker is activated.
 */
export default function SourceList({
  sources,
  activeIndex,
  turnId,
}: {
  sources: Source[];
  activeIndex: number | null;
  turnId: string;
}) {
  if (sources.length === 0) return null;

  return (
    <section className="mt-3" aria-label="Sources for this answer">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Sources
      </h3>
      <ol className="mt-1.5 flex flex-col gap-1.5">
        {sources.map((source, index) => {
          const active = index === activeIndex;
          return (
            <li
              key={source.chunk_id}
              id={`${turnId}-source-${index}`}
              // Focusable so a citation button can move focus here, which is
              // what makes the click-through work for a keyboard as well as a
              // mouse.
              tabIndex={-1}
              className={`rounded-md border px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                active ? "border-primary bg-accent" : "border-border bg-card"
              }`}
            >
              <p className="flex items-baseline gap-2">
                <span className="inline-flex h-5 min-w-5 items-center justify-center rounded bg-accent px-1 text-[11px] font-semibold tabular-nums text-accent-foreground">
                  {index + 1}
                </span>
                <span className="font-medium">{source.title}</span>
                <span className="text-muted-foreground">page {source.page}</span>
              </p>
              <p className="mt-1 text-muted-foreground">{source.snippet}</p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
