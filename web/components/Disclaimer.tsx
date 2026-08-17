import { DISCLAIMER_BODY, DISCLAIMER_LEAD } from "@/lib/copy";

/**
 * Persistent and non-dismissable, by requirement.
 *
 * The PRD names "someone reads the demo as medical advice" as a risk to design
 * against. A disclaimer with a close button is one that is closed once and
 * never seen again, so there is deliberately no way to hide this.
 */
export default function Disclaimer() {
  return (
    <p
      role="note"
      className="rounded-md border border-warn/35 bg-warn/8 px-3 py-2 text-sm text-foreground"
    >
      <strong className="font-semibold">{DISCLAIMER_LEAD}</strong> {DISCLAIMER_BODY}
    </p>
  );
}
