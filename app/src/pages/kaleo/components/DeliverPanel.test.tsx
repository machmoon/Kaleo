// @vitest-environment jsdom
//
// The delivery panel: it asks the engine once what is configured, keeps a
// destination off with the engine's own hint, sends exactly what was typed,
// and reports what came back in the engine's words.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@/lib/cli", () => ({
  listCliTools: vi.fn(async () => []),
  runCli: vi.fn(),
  CliError: class CliError extends Error {
    result?: unknown;
    constructor(message: string, result?: unknown) {
      super(message);
      this.name = "CliError";
      this.result = result;
    }
  },
}));

const deliverConfig = vi.fn();
const deliverRun = vi.fn();
const deliverAuth = vi.fn();
vi.mock("@/lib/silkscreen/client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/silkscreen/client")>(
    "@/lib/silkscreen/client"
  );
  return {
    ...actual,
    deliverConfig: (...args: unknown[]) => deliverConfig(...args),
    deliverRun: (...args: unknown[]) => deliverRun(...args),
    deliverAuth: (...args: unknown[]) => deliverAuth(...args),
  };
});

import { SilkscreenError } from "@/lib/silkscreen/client";
import type { DeliverConfig, SpecReviewBlock, StepResponse } from "@/lib/silkscreen/types";
import { DeliverPanel } from "./DeliverPanel";

function step(partial: Partial<StepResponse> & { step: StepResponse["step"]; stage: string }): StepResponse {
  return {
    session: "s1",
    intent: "a 3.3V regulator",
    files: {},
    next: [],
    shown_in_kicad: false,
    events: [],
    duration_s: 1,
    ...partial,
  };
}

const ROUTED = [step({ step: "place", stage: "placed" }), step({ step: "route", stage: "routed" })];

const READY: DeliverConfig = {
  available: true,
  chat: true,
  gmail: true,
  calendar: true,
  oauth_client: true,
  signed_in: true,
  token: "valid",
  hints: [],
};

const BARE: DeliverConfig = {
  available: true,
  chat: false,
  gmail: false,
  calendar: false,
  oauth_client: false,
  signed_in: false,
  token: "missing",
  hints: [
    "Google Chat: set GOOGLEAPPS_CHAT_WEBHOOK in the service's environment (the service does not read .env)",
    "Gmail and Calendar: set GOOGLEAPPS_CLIENT_ID and GOOGLEAPPS_CLIENT_SECRET in the service's environment (the service does not read .env)",
  ],
};

const NEEDS_SIGN_IN: DeliverConfig = {
  available: true,
  chat: true,
  gmail: false,
  calendar: false,
  oauth_client: true,
  signed_in: false,
  token: "missing",
  hints: [
    "Gmail and Calendar: sign in with Google from Hardy's Send panel (or run `python -m googleapps auth`; token at /x)",
  ],
};

function mount(history: readonly StepResponse[] = ROUTED) {
  return render(
    <DeliverPanel baseUrl="http://127.0.0.1:8081" token="" session="s1" history={history} />
  );
}

beforeEach(() => {
  deliverConfig.mockReset();
  deliverRun.mockReset();
  deliverAuth.mockReset();
});
afterEach(() => cleanup());

describe("DeliverPanel", () => {
  it("asks for the config once and keeps every button off until it arrives", async () => {
    let resolve!: (config: DeliverConfig) => void;
    deliverConfig.mockReturnValue(new Promise<DeliverConfig>((r) => (resolve = r)));
    mount();
    expect(deliverConfig).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", true);
    resolve(READY);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
  });

  it("shows the engine's hint, verbatim, on a destination that is off", async () => {
    deliverConfig.mockResolvedValue(BARE);
    mount();
    await waitFor(() => expect(screen.getAllByTestId("deliver-hint")).toHaveLength(4));
    const hints = screen.getAllByTestId("deliver-hint").map((el) => el.textContent);
    expect(hints[0]).toContain("GOOGLEAPPS_CHAT_WEBHOOK");
    expect(hints[0]).toContain("does not read .env");
    expect(hints[1]).toContain("GOOGLEAPPS_CLIENT_ID");
    expect(screen.queryByTestId("deliver-signin")).toBeNull();
    expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("deliver-email")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("deliver-calendar")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("deliver-spec-review")).toHaveProperty("disabled", true);
    expect(deliverRun).not.toHaveBeenCalled();
  });

  it("opens Google consent from Sign in with Google and refreshes the config", async () => {
    deliverConfig.mockResolvedValue(NEEDS_SIGN_IN);
    deliverAuth.mockResolvedValue(READY);
    mount();
    await waitFor(() => expect(screen.getByTestId("deliver-signin")).toBeTruthy());
    expect(screen.getByTestId("deliver-email")).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByTestId("deliver-signin"));
    expect(deliverAuth).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByTestId("deliver-signin")).toBeNull());
    // Send stays off until addresses are typed; the field itself unlocks.
    expect(screen.getByTestId("deliver-email-to")).toHaveProperty("disabled", false);
    fireEvent.change(screen.getByTestId("deliver-email-to"), {
      target: { value: "lead@example.com" },
    });
    expect(screen.getByTestId("deliver-email")).toHaveProperty("disabled", false);
  });

  it("shows a sign-in refusal verbatim", async () => {
    deliverConfig.mockResolvedValue(NEEDS_SIGN_IN);
    deliverAuth.mockRejectedValue(
      new SilkscreenError("request", "GOOGLEAPPS_CLIENT_ID is not set", { status: 400 })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId("deliver-signin")).toBeTruthy());
    fireEvent.click(screen.getByTestId("deliver-signin"));
    await waitFor(() =>
      expect(screen.getByTestId("deliver-signin-error").textContent).toBe(
        "GOOGLEAPPS_CLIENT_ID is not set"
      )
    );
  });

  it("posts to Chat and reports the engine's answer", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({ session: "s1", chat: { ok: true } });
    mount();
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByTestId("deliver-chat"));
    expect(deliverRun).toHaveBeenCalledTimes(1);
    expect(deliverRun.mock.calls[0][1]).toBe("s1");
    expect(deliverRun.mock.calls[0][2]).toEqual({ chat: true });
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe("Posted the run card to Chat.")
    );
  });

  it("sends exactly the addresses typed, and refuses to send a bad one", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({
      session: "s1",
      email: { ok: true, to: ["lead@example.com"], message_id: "msg-1" },
    });
    mount();
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    const field = screen.getByTestId("deliver-email-to");
    expect(screen.getByTestId("deliver-email")).toHaveProperty("disabled", true);
    fireEvent.change(field, { target: { value: "lead@example.com, nope" } });
    expect(screen.getByTestId("deliver-email")).toHaveProperty("disabled", true);
    expect(screen.getByText(/Not an address: nope/)).toBeTruthy();
    fireEvent.change(field, { target: { value: " lead@example.com , " } });
    fireEvent.click(screen.getByTestId("deliver-email"));
    expect(deliverRun.mock.calls[0][2]).toEqual({ email: ["lead@example.com"] });
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe(
        "Sent the board to lead@example.com (message id msg-1)."
      )
    );
  });

  it("explains the calendar rule from the review, and passes a skip through as a skip", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({
      session: "s1",
      calendar: { ok: false, skipped_reason: "no blockers — nothing scheduled" },
    });
    const { unmount } = mount();
    expect(screen.getByText(/review has not run yet/)).toBeTruthy();
    unmount();

    const reviewed = [...ROUTED, step({ step: "review", stage: "routed", blockers: [] })];
    mount(reviewed);
    expect(screen.getByText(/found no blockers, so there is nothing to book/)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), {
      target: { value: "lead@example.com" },
    });
    fireEvent.click(screen.getByTestId("deliver-calendar"));
    expect(deliverRun.mock.calls[0][2]).toEqual({ schedule: true, attendees: ["lead@example.com"] });
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe(
        "Nothing booked: no blockers — nothing scheduled."
      )
    );
    expect(screen.queryByTestId("deliver-failure")).toBeNull();
  });

  it("shows a refused request verbatim and lets one request finish before the next", async () => {
    deliverConfig.mockResolvedValue(READY);
    let reject!: (error: Error) => void;
    deliverRun.mockReturnValue(new Promise((_, r) => (reject = r)));
    mount();
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByTestId("deliver-chat"));
    fireEvent.click(screen.getByTestId("deliver-chat"));
    expect(deliverRun).toHaveBeenCalledTimes(1);
    reject(new SilkscreenError("request", "delivery needs the run to be 'routed'", { status: 409 }));
    await waitFor(() =>
      expect(screen.getByTestId("deliver-failure").textContent).toBe(
        "delivery needs the run to be 'routed'"
      )
    );
    expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false);
  });

  // ------------------------------------------------------------ spec review

  const AGENDA: SpecReviewBlock = {
    title: "3.3V rail spec review",
    summary: "Two open questions the board cannot settle on its own.",
    items: [
      {
        topic: "Input transient rating",
        why: "U1's absolute maximum is 6 V and the supply is unspecified.",
        minutes: 15,
        blocking: true,
        refs: ["U1"],
      },
      {
        topic: "Silkscreen revision",
        why: "Cosmetic, but nobody has said which revision ships.",
        minutes: 5,
        blocking: false,
      },
    ],
    decisions_needed: ["Pick the input supply"],
  };

  const WITH_AGENDA = [
    ...ROUTED,
    step({ step: "review", stage: "routed", blockers: ["U1 abs max"], spec_review: AGENDA }),
  ];

  it("still finds the agenda once sourcing, order and case have run on top of the review", async () => {
    // The agenda arrives on the `review` envelope and nothing repeats it, so
    // by the time a run is finished it is four steps back. The panel reads
    // the whole history, not the last entry.
    deliverConfig.mockResolvedValue(READY);
    mount([
      ...WITH_AGENDA,
      step({ step: "sourcing", stage: "routed" }),
      step({ step: "order", stage: "routed" }),
      step({ step: "case", stage: "routed" }),
    ]);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    expect(screen.getByTestId("deliver-agenda").textContent).toContain("3.3V rail spec review");
    expect(screen.getAllByTestId("deliver-agenda-item")).toHaveLength(2);
    // And the calendar row still knows the review ran, with its blocker count.
    expect(screen.getByTestId("deliver-panel").textContent).toContain("The review found 1 blocker");
  });

  it("shows the agenda, why each item blocks, and the total minutes before it sends anything", async () => {
    deliverConfig.mockResolvedValue(READY);
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    expect(screen.getByTestId("deliver-agenda").textContent).toContain("3.3V rail spec review");
    expect(screen.getByTestId("deliver-agenda-total").textContent).toBe("20 min");
    const items = screen.getAllByTestId("deliver-agenda-item");
    expect(items).toHaveLength(2);
    expect(items[0].getAttribute("data-topic")).toBe("Input transient rating");
    expect(items[0].getAttribute("data-blocking")).toBe("true");
    expect(items[0].textContent).toContain("absolute maximum is 6 V");
    expect(items[0].textContent).toContain("blocking");
    expect(items[1].getAttribute("data-blocking")).toBe("false");
    expect(screen.getByTestId("deliver-agenda-decisions").textContent).toContain("Pick the input supply");
    expect(deliverRun).not.toHaveBeenCalled();
  });

  it("books the spec review with the attendees typed, and reports the booking", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({
      session: "s1",
      spec_review: { ok: true, meet_uri: "https://meet.google.com/abc" },
    });
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    expect(screen.getByTestId("deliver-spec-review")).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByTestId("deliver-attendees"), {
      target: { value: "lead@example.com, fab@example.com" },
    });
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    expect(deliverRun.mock.calls[0][2]).toEqual({
      spec_review: true,
      attendees: ["lead@example.com", "fab@example.com"],
      when: null,
    });
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe(
        "Booked the spec review. Meet: https://meet.google.com/abc"
      )
    );
  });

  it("blocks a bad attendee before any request reaches the engine", async () => {
    deliverConfig.mockResolvedValue(READY);
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), {
      target: { value: "lead@example.com, nope" },
    });
    expect(screen.getByTestId("deliver-spec-review")).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    expect(deliverRun).not.toHaveBeenCalled();
    expect(screen.getAllByText(/Not an address: nope/).length).toBeGreaterThan(0);
  });

  it("shows a deliberate skip as a result, in both of the engine's words", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({
      session: "s1",
      spec_review: { ok: false, skipped_reason: "no blockers — nothing needs a meeting" },
    });
    const first = mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe(
        "No spec review booked: no blockers — nothing needs a meeting."
      )
    );
    expect(screen.queryByTestId("deliver-failure")).toBeNull();
    first.unmount();

    deliverRun.mockReset();
    deliverRun.mockResolvedValue({
      session: "s1",
      spec_review: { ok: false, skipped_reason: "the review step has not run" },
    });
    mount(ROUTED);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    // No agenda, and the panel says which of the two reasons that is.
    expect(screen.queryByTestId("deliver-agenda")).toBeNull();
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    expect(screen.getByText(/The review step has not run, so there is no agenda yet\./)).toBeTruthy();
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    await waitFor(() =>
      expect(screen.getByTestId("deliver-result").textContent).toBe(
        "No spec review booked: the review step has not run."
      )
    );
  });

  it("keeps a spec-review failure on its own line and sends exactly once per click", async () => {
    deliverConfig.mockResolvedValue(READY);
    let reject!: (error: Error) => void;
    deliverRun.mockReturnValue(new Promise((_, r) => (reject = r)));
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    // A second click is a second calendar invite to real people; one request.
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    fireEvent.click(screen.getByTestId("deliver-calendar"));
    expect(deliverRun).toHaveBeenCalledTimes(1);
    reject(new SilkscreenError("request", "Calendar is not signed in", { status: 409 }));
    await waitFor(() => expect(screen.getAllByTestId("deliver-failure")).toHaveLength(1));
    const failure = screen.getByTestId("deliver-failure");
    expect(failure.textContent).toBe("Calendar is not signed in");
    expect(failure.closest("[data-destination]")?.getAttribute("data-destination")).toBe("spec_review");
    expect(screen.queryByTestId("deliver-result")).toBeNull();
  });
});

describe("DeliverPanel over a failed review", () => {
  const AGENDA: SpecReviewBlock = {
    title: "3.3V rail spec review",
    summary: "One open question.",
    items: [{ topic: "Input transient rating", why: "U1's absolute maximum is 6 V.", minutes: 15, blocking: true }],
  };
  const FAILED = [
    ...ROUTED,
    step({
      step: "review",
      stage: "routed",
      findings: [],
      blockers: [],
      spec_review: AGENDA,
      review: { status: "failed", ran: true, detail: "ModelError: 503", note: "review failed" },
    }),
  ];

  it("keeps both bookings off with the failed sentence, and never says no blockers", async () => {
    deliverConfig.mockResolvedValue(READY);
    mount(FAILED);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    const sentence = "review failed: ModelError: 503 — nothing is known about this board";
    expect(screen.getAllByText(new RegExp(sentence)).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/found no blockers/)).toBeNull();
    expect(screen.getByTestId("deliver-calendar")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("deliver-spec-review")).toHaveProperty("disabled", true);
    // The agenda the engine drafted before the critic gave up is not shown as
    // something to approve: nothing is known about the board.
    expect(screen.queryByTestId("deliver-agenda")).toBeNull();
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    fireEvent.click(screen.getByTestId("deliver-calendar"));
    expect(deliverRun).not.toHaveBeenCalled();
    // Chat and email still work: the board exists, only the review does not.
    expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false);
  });
});

describe("DeliverPanel spec review time", () => {
  const AGENDA: SpecReviewBlock = {
    title: "3.3V rail spec review",
    summary: "One open question the board cannot settle on its own.",
    items: [
      {
        topic: "Input transient rating",
        why: "U1's absolute maximum is 6 V and the supply is unspecified.",
        minutes: 15,
        blocking: true,
        refs: ["U1"],
      },
    ],
  };
  const WITH_AGENDA = [
    ...ROUTED,
    step({ step: "review", stage: "routed", blockers: ["U1 abs max"], spec_review: AGENDA }),
  ];

  it("sends a typed time as RFC 3339 with this machine's offset, and nothing typed as null", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockResolvedValue({ session: "s1", spec_review: { ok: true } });
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    fireEvent.change(screen.getByTestId("deliver-spec-review-when"), { target: { value: "2031-06-08T15:30" } });
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    const sent = deliverRun.mock.calls[0][2] as { when: string | null };
    expect(sent.when).toMatch(/^2031-06-08T15:30:00[+-]\d{2}:\d{2}$/);
    expect(new Date(sent.when as string).getTime()).toBe(new Date(2031, 5, 8, 15, 30).getTime());
    await waitFor(() => expect(screen.getByTestId("deliver-result").textContent).toBe("Booked the spec review."));
    // Cleared again: back to the engine's own slot, sent explicitly.
    fireEvent.change(screen.getByTestId("deliver-spec-review-when"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    await waitFor(() => expect(deliverRun).toHaveBeenCalledTimes(2));
    expect((deliverRun.mock.calls[1][2] as { when: unknown }).when).toBeNull();
  });

  it("shows the engine's refusal of a bad time in the row, verbatim", async () => {
    deliverConfig.mockResolvedValue(READY);
    deliverRun.mockRejectedValue(
      new SilkscreenError("request", "'when' is in the past: '2020-01-01T09:00:00+01:00'", { status: 400 })
    );
    mount(WITH_AGENDA);
    await waitFor(() => expect(screen.getByTestId("deliver-chat")).toHaveProperty("disabled", false));
    fireEvent.change(screen.getByTestId("deliver-attendees"), { target: { value: "lead@example.com" } });
    fireEvent.change(screen.getByTestId("deliver-spec-review-when"), { target: { value: "2020-01-01T09:00" } });
    // The courtesy note says so before the click; the button stays live —
    // the engine is the authority.
    expect(screen.getByText(/That time has already passed/)).not.toBeNull();
    expect(screen.getByTestId("deliver-spec-review")).toHaveProperty("disabled", false);
    fireEvent.click(screen.getByTestId("deliver-spec-review"));
    await waitFor(() => expect(screen.getByTestId("deliver-failure")).not.toBeNull());
    const failure = screen.getByTestId("deliver-failure");
    expect(failure.textContent).toBe("'when' is in the past: '2020-01-01T09:00:00+01:00'");
    expect(failure.closest("[data-destination]")?.getAttribute("data-destination")).toBe("spec_review");
  });
});
