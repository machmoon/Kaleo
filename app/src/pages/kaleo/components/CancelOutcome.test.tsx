// @vitest-environment jsdom
//
// The one rule this card exists to keep: asked, settled and unknown are three
// facts, and none of them may be rendered as another. A run this window
// stopped listening to is not a run that stopped, and a run nobody was
// charged for is not a run nobody was told the price of.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Cancellation, MeteringBlock } from "@/hooks/useSilkscreenRun";
import { CancelOutcome, costLine } from "./CancelOutcome";

afterEach(cleanup);

const BASE: Cancellation = {
  state: "asked",
  runId: "run_abc123",
  headline: null,
  notStoppable: null,
  abortsAt: null,
  runState: null,
  metering: null,
  detail: null,
};

// The service's own words (`service/runs.py::cancel`), not a paraphrase.
const NOT_STOPPABLE =
  "A model call or a solve already under way runs to completion and is still billed; only the run stops.";

function show(cancellation: Cancellation | null) {
  render(<CancelOutcome cancellation={cancellation} onDismiss={() => {}} />);
  return screen.getByTestId("cancel-outcome");
}

describe("CancelOutcome: the three states stay three", () => {
  it("asked says it is asking, and never that the run stopped", () => {
    const card = show(BASE);
    expect(card.dataset.state).toBe("asked");
    expect(screen.getByTestId("cancel-headline").textContent).toBe(
      "Asking the engine to stop…"
    );
    // No cost while it is still spending, and no dismiss on a live question.
    expect(screen.queryByTestId("cancel-cost")).toBeNull();
    expect(screen.queryByTestId("cancelled-dismiss")).toBeNull();
  });

  it("settled renders the engine's headline verbatim, never a rewrite", () => {
    show({
      ...BASE,
      state: "settled",
      runState: "cancelled",
      headline: "cancelled -- the run stops at its next pipeline event",
      notStoppable: NOT_STOPPABLE,
      abortsAt: "the next event the pipeline reports",
      metering: { enabled: false, state: "off", reason: "metering is off" },
    });
    expect(screen.getByTestId("cancel-headline").textContent).toBe(
      "cancelled -- the run stops at its next pipeline event"
    );
    expect(screen.getByTestId("cancel-not-stoppable").textContent).toBe(NOT_STOPPABLE);
    expect(screen.getByTestId("cancel-aborts-at").textContent).toBe(
      "Stops at the next event the pipeline reports."
    );
    expect(screen.queryByTestId("cancel-unresolved")).toBeNull();
  });

  it("a run that had already ended keeps the engine's word for it", () => {
    const card = show({
      ...BASE,
      state: "settled",
      runState: "done",
      headline: "already done; nothing was stopped",
      // in_flight was false, so the hook carries no aborts_at: promising a
      // stopping point for something that already finished is a lie.
      abortsAt: null,
    });
    expect(card.dataset.runState).toBe("done");
    expect(screen.getByTestId("cancel-headline").textContent).toBe(
      "already done; nothing was stopped"
    );
    expect(screen.queryByTestId("cancel-aborts-at")).toBeNull();
  });

  it("unknown never says cancelled, and names the run so it can be checked", () => {
    const card = show({
      ...BASE,
      state: "unknown",
      detail: "Could not reach the silkscreen engine.",
    });
    expect(card.dataset.state).toBe("unknown");
    const headline = screen.getByTestId("cancel-headline").textContent ?? "";
    expect(headline).not.toMatch(/cancelled|stopped/i);
    const unresolved = screen.getByTestId("cancel-unresolved").textContent ?? "";
    expect(unresolved).toContain("Could not reach the silkscreen engine.");
    expect(unresolved).toContain("It may still be running as run_abc123.");
    // Not known to have settled, so not priced.
    expect(screen.queryByTestId("cancel-cost")).toBeNull();
  });

  it("a run still going when we stopped watching is unknown, not cancelled", () => {
    show({
      ...BASE,
      state: "unknown",
      runState: "running",
      headline: "cancelled -- the run stops at its next pipeline event",
      detail:
        "It had not reached its next pipeline event while this window was watching, " +
        "so it is still going and still being billed.",
    });
    // The engine's headline said "cancelled"; the poll said otherwise. The
    // poll wins, because it is the later fact.
    expect(screen.getByTestId("cancel-headline").textContent).toBe(
      "Asked the engine to stop — it has not confirmed."
    );
    expect(screen.getByTestId("cancel-unresolved").textContent).toContain(
      "still being billed"
    );
  });

  it("no cancellation at all is its own state, not a settled one", () => {
    const card = show(null);
    expect(card.dataset.state).toBe("unasked");
    expect(card.textContent).toContain("never had a name for it");
  });
});

describe("costLine: not charged and not told are different facts", () => {
  const enabled = (patch: Partial<MeteringBlock>): MeteringBlock => ({
    enabled: true,
    state: "committed",
    ...patch,
  });

  it("no block at all says the engine did not say", () => {
    expect(costLine(null)).toBe("The engine did not say what this cost.");
  });

  it("metering off quotes the engine's reason and never implies a charge", () => {
    const line = costLine({
      enabled: false,
      state: "off",
      reason:
        "metering is off (KALEO_METERING is not set); this run was not charged against any ledger",
    });
    expect(line).toBe(
      "Not charged — metering is off (KALEO_METERING is not set); this run was not charged against any ledger."
    );
    expect(line).not.toMatch(/mKCU|USD/);
  });

  it("a charge the ledger did not record is neither charged nor free", () => {
    const line = costLine(
      enabled({ state: "unrecorded", reason: "the ledger was unwritable" })
    );
    expect(line).toContain("the ledger did not record it");
    expect(line).toContain("the ledger was unwritable");
  });

  it("metered with no amount does not invent one", () => {
    expect(costLine(enabled({}))).toBe(
      "Metered, but this window was not told the amount."
    );
  });

  it("zero gets a word, not a numeral", () => {
    // vitest's getStateString rule: the empty case is named, and no category
    // is ever printed as a zero.
    expect(costLine(enabled({ charged_mkcu: 0, cost_cents: 0 }))).toBe(
      "Billed nothing — the run stopped before it used any credit."
    );
  });

  it("a real charge shows the engine's integer unit and the money", () => {
    const line = costLine(enabled({ charged_mkcu: 1500, cost_cents: 42 }));
    expect(line).toContain("Billed 1500 mKCU");
    expect(line).toContain("(0.42 USD)");
    expect(line).toContain("about 1.50 min of engine time");
  });

  it("a charge with no money figure omits the money rather than printing zero", () => {
    const line = costLine(enabled({ charged_mkcu: 200, cost_cents: 0 }));
    expect(line).toContain("Billed 200 mKCU");
    expect(line).not.toContain("USD");
  });
});
