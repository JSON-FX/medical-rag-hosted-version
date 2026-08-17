import type { DoneTelemetry, Frame, MetaTelemetry, Source } from "./types";

export type TurnKind = "pending" | "answer" | "decline" | "error";

export interface Turn {
  role: "user" | "assistant";
  text: string;
  kind: TurnKind;
  sources: Source[];
  declineReason: string | null;
  errorCode: string | null;
  /**
   * A recovery sentence written for this specific failure — the server's own
   * error body, which `errors.py` writes for a reader rather than for a log.
   */
  errorMessage: string | null;
  truncated: boolean;
  done: boolean;
  /**
   * The telemetry strip's two halves, kept separate because they arrive
   * separately: `meta` after retrieval, `done` after the stream ends. Either
   * may be absent — on a retrieval failure the server emits `error` then
   * `done` and no `meta` at all — so the strip renders whatever it has.
   */
  meta: MetaTelemetry | null;
  doneTelemetry: DoneTelemetry | null;
}

export interface ChatState {
  turns: Turn[];
  streaming: boolean;
}

export type ChatAction =
  | { type: "ask"; question: string }
  | { type: "frame"; frame: Frame }
  // The stream did not finish cleanly: either fetch rejected, or the body
  // ended without a terminal frame. Distinct from an `error` frame, which
  // means the API is up and told us what went wrong.
  | { type: "failed"; message: string };

/**
 * Does this frame end the turn?
 *
 * The page needs this to tell a stream that finished from one that was cut
 * off, and it must agree exactly with which cases below set `done: true` — so
 * the two live next to each other rather than being restated by the caller.
 */
export function isTerminalFrame(frame: Frame): boolean {
  return frame.type === "done" || frame.type === "error";
}

export const initialChatState: ChatState = {
  turns: [],
  streaming: false,
};

function userTurn(text: string): Turn {
  return {
    role: "user",
    text,
    kind: "answer",
    sources: [],
    declineReason: null,
    errorCode: null,
    errorMessage: null,
    truncated: false,
    done: true,
    meta: null,
    doneTelemetry: null,
  };
}

function pendingTurn(): Turn {
  return {
    role: "assistant",
    text: "",
    kind: "pending",
    sources: [],
    declineReason: null,
    errorCode: null,
    errorMessage: null,
    truncated: false,
    done: false,
    meta: null,
    doneTelemetry: null,
  };
}

/** Replace the last turn via `update`, leaving every other turn untouched. */
function patchLast(state: ChatState, update: (turn: Turn) => Turn): ChatState {
  if (state.turns.length === 0) return state;
  const turns = state.turns.slice();
  turns[turns.length - 1] = update(turns[turns.length - 1]);
  return { ...state, turns };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "ask") {
    return {
      ...state,
      turns: [...state.turns, userTurn(action.question), pendingTurn()],
      streaming: true,
    };
  }

  if (action.type === "failed") {
    // `text` is deliberately left alone. Whatever streamed before the break is
    // real model output and the user watched it arrive; discarding it would
    // make a cut-off answer indistinguishable from one that never started.
    return {
      ...patchLast(state, (turn) => ({
        ...turn,
        kind: "error",
        errorCode: "transport",
        errorMessage: action.message,
        done: true,
      })),
      streaming: false,
    };
  }

  const frame = action.frame;

  switch (frame.type) {
    case "meta":
      // The first half of the strip, and on a refusal the whole reason it
      // exists: the gate decision is on screen BEFORE the decline text
      // renders, so a reader sees why before what.
      return patchLast(state, (turn) => ({ ...turn, meta: frame.telemetry }));

    case "sources":
      // Sources arrive if and only if the turn will be an answer, and they
      // arrive immediately BEFORE the first token — not after the answer. That
      // ordering is what lets citations be live for the whole time the answer
      // is streaming rather than resolving only once it ends.
      return patchLast(state, (turn) => ({
        ...turn,
        kind: "answer",
        sources: frame.items,
      }));

    case "token":
      return patchLast(state, (turn) => ({
        ...turn,
        // Still unclassified when text starts arriving means no sources came
        // first, which by the invariant above makes this a decline. Errors are
        // left alone: once a turn has failed, stray tokens do not un-fail it.
        kind: turn.kind === "pending" ? "decline" : turn.kind,
        text: turn.text + frame.text,
      }));

    case "done":
      return {
        ...patchLast(state, (turn) => ({
          ...turn,
          kind: frame.was_declined ? "decline" : turn.kind === "pending" ? "answer" : turn.kind,
          declineReason: frame.decline_reason,
          truncated: frame.telemetry.truncated,
          // Merged alongside `meta`, never replacing it. The two halves carry
          // different fields — the gate and retrieval latency here, TTFT and
          // the serving provider there — and a strip that dropped the first on
          // receiving the second would blank the gate at the moment the answer
          // finished.
          doneTelemetry: frame.telemetry,
          done: true,
        })),
        streaming: false,
      };

    case "error":
      // Terminal in its own right. The server normally sends `done` straight
      // after, but if the connection drops in between, waiting for it would
      // leave the question box disabled with nothing left to arrive.
      //
      // `errorMessage` is kept, which is the one place this diverges from the
      // component it ports from. There, a frame's message was a diagnostic
      // string and the UI wrote its own recovery copy. Here `errors.py` writes
      // these for a reader on purpose — "This demo runs on free tiers with no
      // availability guarantee", and the manifest reason a broken deployment
      // needs to state in a browser. Substituting generic copy would throw
      // away the one sentence that says what actually happened.
      return {
        ...patchLast(state, (turn) => ({
          ...turn,
          kind: "error",
          errorCode: frame.code,
          errorMessage: frame.message,
          done: true,
        })),
        streaming: false,
      };
  }
}
