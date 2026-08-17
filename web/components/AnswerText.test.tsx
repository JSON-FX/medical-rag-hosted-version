import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import AnswerText from "./AnswerText";

afterEach(() => {
  cleanup();
  // Spies here are on console.error; without this they accumulate calls
  // across tests and "logged nothing" passes or fails by test order.
  vi.restoreAllMocks();
});

function renderAnswer(text: string, sourceCount: number, activeIndex: number | null = null) {
  const onCite = vi.fn();
  render(
    <AnswerText
      text={text}
      sourceCount={sourceCount}
      activeIndex={activeIndex}
      onCite={onCite}
    />,
  );
  return onCite;
}

test("a marker within range renders a button that resolves to sources[n-1]", () => {
  // AC #2. The whole citation feature is this one line of arithmetic.
  const onCite = renderAnswer("The dose is 500 mg [1].", 2);
  const button = screen.getByRole("button", { name: "Show source 1" });
  button.click();
  expect(onCite).toHaveBeenCalledWith(0);
});

test("the third marker resolves to index two", () => {
  const onCite = renderAnswer("See [3].", 3);
  screen.getByRole("button", { name: "Show source 3" }).click();
  expect(onCite).toHaveBeenCalledWith(2);
});

test("every marker in the text becomes its own control", () => {
  renderAnswer("First [1], second [2], and again [1].", 2);
  expect(screen.getAllByRole("button")).toHaveLength(3);
});

test("a marker past the end of sources renders as plain text, not a dead button", () => {
  // Small models occasionally invent a citation number, and an affordance
  // that does nothing is worse than no affordance.
  vi.spyOn(console, "error").mockImplementation(() => {});
  renderAnswer("Supported by [3].", 2);
  expect(screen.queryByRole("button")).toBeNull();
  expect(document.body.textContent).toContain("[3]");
});

test("an unresolvable citation is logged as the bug it is", () => {
  // ARCHITECTURE.md §7: "An unresolvable citation is a bug and is logged as
  // one." Degrading gracefully satisfies the reader-facing half; without this
  // the other half is silently unmet.
  const logged = vi.spyOn(console, "error").mockImplementation(() => {});
  renderAnswer("Supported by [4].", 1);
  expect(logged).toHaveBeenCalledOnce();
  expect(String(logged.mock.calls[0][0])).toContain("unresolvable citation");
});

test("a zero marker is unresolvable and does not index backwards", () => {
  // [0] would map to sources[-1]. Bounds are checked at both ends.
  vi.spyOn(console, "error").mockImplementation(() => {});
  renderAnswer("See [0].", 2);
  expect(screen.queryByRole("button")).toBeNull();
});

test("a resolvable answer logs nothing", () => {
  const logged = vi.spyOn(console, "error").mockImplementation(() => {});
  renderAnswer("Both [1] and [2] agree.", 2);
  expect(logged).not.toHaveBeenCalled();
});

test("text with no markers renders unchanged", () => {
  renderAnswer("I can't answer that from the labels this system has indexed.", 0);
  expect(document.body.textContent).toBe(
    "I can't answer that from the labels this system has indexed.",
  );
  expect(screen.queryByRole("button")).toBeNull();
});

test("the text around a marker is preserved exactly", () => {
  renderAnswer("Start with 500 mg [1] twice daily.", 1);
  expect(document.body.textContent).toBe("Start with 500 mg 1 twice daily.");
});

test("a partially streamed marker is left alone until it completes", () => {
  // Tokens arrive mid-marker while streaming; "[1" is not yet a citation and
  // must render as the characters that arrived rather than disappearing.
  renderAnswer("The dose is 500 mg [1", 1);
  expect(document.body.textContent).toBe("The dose is 500 mg [1");
  expect(screen.queryByRole("button")).toBeNull();
});

test("the active citation is distinguished from the others", () => {
  renderAnswer("Both [1] and [2].", 2, 1);
  const [first, second] = screen.getAllByRole("button");
  expect(first.className).not.toBe(second.className);
});

test("citations are reachable and labelled for a screen reader", () => {
  // AC #5. A bare "1" is meaningless read aloud.
  renderAnswer("See [2].", 2);
  const button = screen.getByRole("button", { name: "Show source 2" });
  expect(button.tagName).toBe("BUTTON");
  expect(button.className).toContain("focus-visible:ring");
});
