import { afterEach, expect, test, vi } from "vitest";

import { streamChat } from "./api";
import type { Frame } from "./types";

function ndjsonResponse(lines: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(line + "\n"));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function mockFetch(response: Response | (() => Promise<Response>)) {
  const impl = typeof response === "function" ? response : async () => response;
  vi.stubGlobal("fetch", vi.fn(impl));
}

async function collect(signal?: AbortSignal): Promise<Frame[]> {
  const out: Frame[] = [];
  for await (const frame of streamChat({ question: "q", signal })) out.push(frame);
  return out;
}

afterEach(() => vi.unstubAllGlobals());

test("a 200 yields the parsed frames", async () => {
  mockFetch(
    ndjsonResponse([
      '{"type":"token","text":"500 "}',
      '{"type":"token","text":"mg"}',
      '{"type":"done","telemetry":{"latency":{"ttft_ms":12},"total_tokens":2,"provider":"groq","truncated":false},"was_declined":false,"decline_reason":null}',
    ]),
  );
  const frames = await collect();
  expect(frames.map((f) => f.type)).toEqual(["token", "token", "done"]);
});

test("it posts the question same-origin with no base URL", async () => {
  mockFetch(ndjsonResponse(['{"type":"token","text":"x"}']));
  await collect();
  const [url, init] = vi.mocked(fetch).mock.calls[0];
  expect(url).toBe("/api/chat");
  expect(JSON.parse(String(init?.body))).toEqual({ question: "q" });
});

test("the request body carries the question and nothing else", async () => {
  // Single-turn by design (PRD non-goal): the transcript is a rendering
  // choice, and no history is sent to the model.
  mockFetch(ndjsonResponse(['{"type":"token","text":"x"}']));
  await collect();
  const [, init] = vi.mocked(fetch).mock.calls[0];
  expect(Object.keys(JSON.parse(String(init?.body)))).toEqual(["question"]);
});

test("a 429 becomes an error frame carrying the server's own message", async () => {
  mockFetch(
    jsonResponse(
      429,
      {
        code: "rate_limited",
        message: "You have made too many requests. Please try again in about 1 minute(s).",
      },
      { "Retry-After": "42" },
    ),
  );
  const frames = await collect();
  expect(frames).toHaveLength(1);
  expect(frames[0]).toMatchObject({ type: "error", code: "rate_limited" });
  expect((frames[0] as { message: string }).message).toContain("minute");
});

test("a rate limit with no body still names the wait rather than the status", async () => {
  // PRD F15: never a raw 429. If the body is missing or unparseable the
  // header is the only thing left that can say how long to wait.
  mockFetch(new Response("gateway error", { status: 429, headers: { "Retry-After": "7200" } }));
  const [frame] = await collect();
  const message = (frame as { message: string }).message;
  expect(message).toContain("2 hour(s)");
  expect(message).not.toContain("429");
});

test("a 503 becomes an error frame with its code intact", async () => {
  mockFetch(
    jsonResponse(503, {
      code: "index_unavailable",
      message: "index was built by 'gemini-embedding-001' but this service is configured with 'x'",
    }),
  );
  const [frame] = await collect();
  expect(frame).toMatchObject({ type: "error", code: "index_unavailable" });
});

test("a 400 becomes an error frame rather than a thrown exception", async () => {
  mockFetch(jsonResponse(400, { code: "invalid_request", message: "question must be a string" }));
  const [frame] = await collect();
  expect(frame).toMatchObject({ type: "error", code: "invalid_request" });
});

test("an error response is never parsed as NDJSON", async () => {
  // The failure this guards: a JSON error body is a single object, so feeding
  // it to readFrames throws on the first JSON.parse of a partial line and the
  // caller sees a crash instead of the message the server wrote for them.
  mockFetch(jsonResponse(503, { code: "all_providers_unavailable", message: "both down" }));
  await expect(collect()).resolves.toHaveLength(1);
});

test("a fetch rejection becomes an error frame, not a rejection", async () => {
  mockFetch(async () => {
    throw new TypeError("Failed to fetch");
  });
  const [frame] = await collect();
  expect(frame).toMatchObject({ type: "error", code: "transport" });
});

test("an abort yields nothing at all", async () => {
  // Cancelling one question must not paint an error on the next.
  mockFetch(async () => {
    throw new DOMException("aborted", "AbortError");
  });
  const controller = new AbortController();
  controller.abort();
  expect(await collect(controller.signal)).toEqual([]);
});

test("the abort signal is passed to fetch", async () => {
  mockFetch(ndjsonResponse(['{"type":"token","text":"x"}']));
  const controller = new AbortController();
  await collect(controller.signal);
  const [, init] = vi.mocked(fetch).mock.calls[0];
  expect(init?.signal).toBe(controller.signal);
});

test("a body that stops mid-frame reports an interruption", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('{"type":"token","text":"partial"}\n{"type":"tok'));
      controller.close();
    },
  });
  mockFetch(new Response(body, { status: 200 }));
  const frames = await collect();
  expect(frames[0]).toMatchObject({ type: "token", text: "partial" });
  expect(frames[1]).toMatchObject({ type: "error", code: "stream_interrupted" });
});

test("a 200 with no body reports a transport failure", async () => {
  mockFetch(new Response(null, { status: 200 }));
  const [frame] = await collect();
  expect(frame).toMatchObject({ type: "error", code: "transport" });
});
