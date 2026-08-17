import { expect, test } from "vitest";

import { readFrames } from "./ndjson";
import type { Frame } from "./types";

/** Build a ReadableStream that emits exactly the given string chunks. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]): Promise<Frame[]> {
  const out: Frame[] = [];
  for await (const frame of readFrames(streamOf(chunks))) out.push(frame);
  return out;
}

test("parses one frame per line", async () => {
  const frames = await collect([
    '{"type":"meta","session_id":"abc"}\n{"type":"token","text":"hi"}\n',
  ]);
  expect(frames).toEqual([
    { type: "meta", session_id: "abc" },
    { type: "token", text: "hi" },
  ]);
});

test("reassembles a frame split across two chunks", async () => {
  // The failure mode that ships green against short fixtures: a chunk
  // boundary lands mid-JSON, and naive split("\n") + JSON.parse throws.
  const frames = await collect(['{"type":"token","te', 'xt":"split"}\n']);
  expect(frames).toEqual([{ type: "token", text: "split" }]);
});

test("handles a boundary that lands exactly on the newline", async () => {
  const frames = await collect(['{"type":"token","text":"a"}', '\n{"type":"token","text":"b"}\n']);
  expect(frames).toEqual([
    { type: "token", text: "a" },
    { type: "token", text: "b" },
  ]);
});

test("emits a final frame with no trailing newline", async () => {
  const frames = await collect(['{"type":"token","text":"last"}']);
  expect(frames).toEqual([{ type: "token", text: "last" }]);
});

test("ignores blank lines", async () => {
  const frames = await collect(['\n{"type":"token","text":"x"}\n\n']);
  expect(frames).toEqual([{ type: "token", text: "x" }]);
});

test("cancels the body when the consumer stops early", async () => {
  // The user navigates away mid-answer and ChatWindow breaks out of the loop.
  // Releasing the lock without cancelling leaves Django's streaming response
  // unread and the connection held open until it times out.
  let cancelled = false;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('{"type":"token","text":"a"}\n'));
      controller.enqueue(encoder.encode('{"type":"token","text":"b"}\n'));
      controller.close();
    },
    cancel() {
      cancelled = true;
    },
  });

  for await (const frame of readFrames(stream)) {
    void frame;
    break;
  }
  expect(cancelled).toBe(true);
});

test("does not cancel a stream it read to the end", async () => {
  let cancelled = false;
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('{"type":"token","text":"a"}\n'));
      controller.close();
    },
    cancel() {
      cancelled = true;
    },
  });

  const out: Frame[] = [];
  for await (const frame of readFrames(stream)) out.push(frame);
  expect(out).toHaveLength(1);
  expect(cancelled).toBe(false);
});

test("does not split a multi-byte character across chunks", async () => {
  // An em dash is three UTF-8 bytes; the decoder must stream, not decode
  // each chunk independently, or the character is corrupted.
  const encoder = new TextEncoder();
  const bytes = encoder.encode('{"type":"token","text":"—"}\n');
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, 25));
      controller.enqueue(bytes.slice(25));
      controller.close();
    },
  });
  const out: Frame[] = [];
  for await (const frame of readFrames(stream)) out.push(frame);
  expect(out).toEqual([{ type: "token", text: "—" }]);
});
