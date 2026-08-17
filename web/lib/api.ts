import { readFrames } from "./ndjson";
import type { Frame } from "./types";

/**
 * POST a question and yield NDJSON frames as they arrive.
 *
 * Same-origin, deliberately: in production the frontend and the API are two
 * services in one Vercel project sharing a domain, and in development a Next
 * rewrite proxies `/api/*` to port 8000. So there is no base URL here and no
 * `NEXT_PUBLIC_API_URL` anywhere — see `next.config.ts`.
 *
 * Every failure comes back as an `error` frame rather than a thrown exception,
 * including the ones that never reach the stream. A 429, a 400 and a 503 are
 * plain JSON bodies, not frame streams, so `readFrames` would throw on their
 * first `JSON.parse`; branching on `response.ok` first and synthesising a
 * frame gives the caller exactly one shape to render, which is the same reason
 * the server runs retrieval inside its generator.
 */
export async function* streamChat(options: {
  question: string;
  signal?: AbortSignal;
}): AsyncGenerator<Frame> {
  let response: Response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: options.question }),
      signal: options.signal,
    });
  } catch (error) {
    // An abort is the caller's own doing; it must not be reported as a
    // failure, or cancelling one question paints an error on the next.
    if (isAbort(error)) return;
    yield {
      type: "error",
      code: "transport",
      message: "Could not reach the service. Check your connection and ask again.",
    };
    return;
  }

  if (!response.ok) {
    yield await errorFrameFor(response);
    return;
  }

  if (!response.body) {
    yield {
      type: "error",
      code: "transport",
      message: "The service replied with no answer stream.",
    };
    return;
  }

  try {
    yield* readFrames(response.body);
  } catch (error) {
    if (isAbort(error)) return;
    // The body ended mid-frame, or was not NDJSON at all. Whatever streamed
    // before the break stays on screen; this only says it stopped.
    yield {
      type: "error",
      code: "stream_interrupted",
      message: "The connection dropped before the answer finished. Ask again to retry.",
    };
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Turn a non-200 into an `error` frame.
 *
 * The server writes these messages for a reader — `errors.py` phrases a rate
 * limit as "try again in about 2 minute(s)" rather than returning a raw 429 —
 * so its body is used as-is whenever there is one. `Retry-After` is the
 * fallback for the case where there is not: PRD F15 is that a visitor is never
 * shown a bare status code, and a rate-limit message with no wait in it is
 * exactly that.
 */
async function errorFrameFor(response: Response): Promise<Frame> {
  let code = `http_${response.status}`;
  let message = "";

  try {
    const body = await response.json();
    if (typeof body?.code === "string") code = body.code;
    if (typeof body?.message === "string") message = body.message;
  } catch {
    // Not a JSON body; fall through to the synthesised message.
  }

  if (!message) {
    const wait = retryAfterPhrase(response.headers.get("Retry-After"));
    message =
      response.status === 429
        ? `You have made too many requests. Please try again${wait ? ` in about ${wait}` : " shortly"}.`
        : "Something went wrong handling this question. Please try again shortly.";
  }

  return { type: "error", code, message };
}

/** Seconds into the unit a person would actually use, matching `errors.py`. */
function retryAfterPhrase(header: string | null): string | null {
  const seconds = Number(header);
  if (!header || !Number.isFinite(seconds) || seconds <= 0) return null;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hour(s)`;
  if (seconds >= 60) return `${Math.round(seconds / 60)} minute(s)`;
  return `${Math.round(seconds)} second(s)`;
}
