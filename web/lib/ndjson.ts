import type { Frame } from "./types";

/**
 * Yield one parsed frame per newline-delimited line of an NDJSON stream.
 *
 * ReadableStream chunks do not respect line boundaries: one frame can arrive
 * split across two reads, several can arrive in one, and the last may have no
 * trailing newline. So the buffer keeps whatever follows the final newline and
 * carries it into the next chunk.
 *
 * TextDecoder is used in streaming mode ({stream: true}) for the same reason the
 * buffer keeps a partial line: a multi-byte character can straddle a chunk boundary,
 * and decoding each chunk independently would corrupt it.
 */
export async function* readFrames(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<Frame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let drained = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // The trailing element is either an incomplete frame or "" — either way
      // it is not ready to parse, so it stays in the buffer.
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line.trim()) yield JSON.parse(line) as Frame;
      }
    }

    buffer += decoder.decode();
    if (buffer.trim()) yield JSON.parse(buffer) as Frame;
    drained = true;
  } finally {
    // A consumer that `break`s out of `for await` — or throws, or aborts —
    // disposes this generator here with the body still open. Releasing the
    // lock alone leaves the response unread and the connection held; cancel()
    // tells the stream nobody is coming back for the rest.
    if (!drained) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
