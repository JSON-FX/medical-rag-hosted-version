/**
 * UI-authored PRESENTATION only.
 *
 * The decline sentences themselves come from the server as token text and are
 * rendered exactly as received — they are authored in `rag_core/prompts.py`
 * so that the wording is identical here and in the evaluation harness, and a
 * frontend that wrote its own would quietly break that. What lives here are
 * the short labels ABOVE that text, never a replacement for it.
 */

export const PRODUCT_NAME = "Medical RAG";

export const TAGLINE =
  "Ask about metformin, atenolol or amoxicillin. Answers come only from those "
  + "three FDA labels and cite the page they came from.";

/**
 * Non-dismissable by requirement. The PRD names "someone reads the demo as
 * medical advice" as a risk to design against, and a disclaimer with a close
 * button is one that is closed once and never seen again.
 */
export const DISCLAIMER_LEAD = "Not a clinical tool.";
export const DISCLAIMER_BODY =
  "This is an engineering demo over three public drug labels — do not use it "
  + "for medical decisions.";

/** The short label above a refusal. The sentence under it is the server's. */
export const DECLINE_LABELS: Record<string, string> = {
  empty_corpus: "Nothing indexed to search",
  off_domain: "Outside this corpus",
  weak_unsupported: "Not a confident match",
  insufficient_context: "Covered, but not in this detail",
};

export const FALLBACK_DECLINE_LABEL = "Declined to answer";

export function declineLabel(reason: string | null): string {
  if (!reason) return FALLBACK_DECLINE_LABEL;
  return DECLINE_LABELS[reason] ?? FALLBACK_DECLINE_LABEL;
}

/**
 * A refusal is a decision, so it says so. This is the sentence that makes the
 * difference between "the system declined" and "the system broke" — PRD
 * success criterion 2 is that a refusal reads as deliberate, not broken.
 */
export const DECLINE_RATIONALE =
  "This is the designed behaviour, not a failure: the confidence gate below "
  + "decided the retrieved text does not support an answer, and refusing beats "
  + "guessing.";

export const ERROR_LABELS: Record<string, string> = {
  rate_limited: "Rate limited",
  index_unavailable: "This deployment cannot serve queries",
  all_providers_unavailable: "No answer provider available",
  provider_unavailable: "A service is temporarily unavailable",
  provider_error: "A service rejected the request",
  invalid_request: "That question could not be read",
  internal_error: "Something went wrong",
  stream_interrupted: "The answer was cut off",
  transport: "Could not reach the service",
};

export const FALLBACK_ERROR_LABEL = "Something went wrong";

export function errorLabel(code: string | null): string {
  if (!code) return FALLBACK_ERROR_LABEL;
  return ERROR_LABELS[code] ?? FALLBACK_ERROR_LABEL;
}

/** The stream ended without a terminal frame; there is no code to key off. */
export const STREAM_CUT_OFF =
  "The connection dropped before the answer finished. Ask again to retry — "
  + "nothing above was lost.";

export const TRUNCATED_NOTE =
  "This answer is incomplete — the provider stopped partway through.";

export interface Example {
  question: string;
  /** What this example is for. Shown, because a hidden refusal is not discoverable. */
  note: string;
  /** Whether this one is expected to be refused. */
  refuses: boolean;
}

/**
 * The refusal examples are drawn from `src/ingest/fixtures/manifest.json`,
 * which records for each drug the axes MEASURED absent from its assembled
 * text — atenolol's include `pregnancy`, metformin's include `overdose`.
 *
 * That measurement is what makes the label honest. A near-miss invented on the
 * theme of "something the corpus probably lacks" may well be answerable, and
 * an example promising a refusal that produces an answer is worse than having
 * no examples at all.
 */
export const EXAMPLES: Example[] = [
  {
    question: "What is the adult starting dose of metformin?",
    note: "Answered and cited",
    refuses: false,
  },
  {
    question: "When is atenolol contraindicated?",
    note: "Answered and cited",
    refuses: false,
  },
  {
    question: "Is atenolol safe to take during pregnancy?",
    note: "Refused — the corpus covers atenolol, but not pregnancy",
    refuses: true,
  },
  {
    question: "What is the capital of France?",
    note: "Refused — nothing to do with these labels",
    refuses: true,
  },
];

export const EXAMPLES_HEADING = "Try one of these";

export const EXAMPLES_NOTE =
  "Two of these are refused on purpose. Watching one refuse is the fastest way "
  + "to see what this system is actually for.";
