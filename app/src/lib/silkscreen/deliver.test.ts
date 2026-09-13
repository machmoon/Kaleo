// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import {
  badAddresses,
  deliverable,
  describeCalendar,
  describeChat,
  describeDelivery,
  describeEmail,
  hintFor,
  looksLikeAddress,
  needsGoogleSignIn,
  parseAddresses,
  reviewState,
  scheduleNote,
  agendaMinutes,
  blockingItems,
  describeSpecReview,
  readSummaryMode,
  specReviewFrom,
  specReviewOffer,
  summaryFields,
  writeSummaryMode,
} from "./deliver";
import type { DeliverConfig, SpecReviewBlock, StepResponse } from "./types";

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

describe("parseAddresses", () => {
  it("splits on commas, semicolons and whitespace, dropping blanks and repeats", () => {
    expect(parseAddresses(" lead@example.com, fab@example.com;lead@example.com\n")).toEqual([
      "lead@example.com",
      "fab@example.com",
    ]);
    expect(parseAddresses("")).toEqual([]);
    expect(parseAddresses(" , ")).toEqual([]);
  });
});

describe("looksLikeAddress", () => {
  it("mirrors the engine's header-safe subset", () => {
    expect(looksLikeAddress("lead@example.com")).toBe(true);
    expect(looksLikeAddress("first.last+tag@sub.example.co")).toBe(true);
    expect(looksLikeAddress("not an address")).toBe(false);
    expect(looksLikeAddress("lead@localhost")).toBe(false);
    expect(looksLikeAddress("a@b@c.com")).toBe(false);
    expect(looksLikeAddress("Lead <lead@example.com>")).toBe(false);
    expect(badAddresses(["lead@example.com", "nope"])).toEqual(["nope"]);
  });
});

describe("deliverable and reviewState", () => {
  it("needs copper, and knows unreviewed from clean", () => {
    expect(deliverable([])).toBe(false);
    expect(deliverable([step({ step: "place", stage: "placed" })])).toBe(false);
    const routed = [step({ step: "place", stage: "placed" }), step({ step: "route", stage: "routed" })];
    expect(deliverable(routed)).toBe(true);
    expect(reviewState(routed)).toEqual({ reviewed: false, blockers: 0, failed: false, failure: null });
    const reviewed = [...routed, step({ step: "review", stage: "routed", blockers: ["VIN has no bulk cap"] })];
    expect(reviewState(reviewed)).toEqual({ reviewed: true, blockers: 1, failed: false, failure: null });
    // Delivery is still possible after review: the stage stays routed.
    expect(deliverable(reviewed)).toBe(true);
  });

  it("asks whether route has run at all, not what ran most recently", () => {
    // Every step after route carries the run forward, and the panel has to
    // survive all of them: the spec-review destination books a meeting off
    // the review's own agenda, so vanishing the moment review lands would
    // close the panel exactly when it first has something to send.
    const run = [
      step({ step: "place", stage: "placed" }),
      step({ step: "route", stage: "routed" }),
    ];
    for (const later of ["review", "sourcing", "order", "case"] as const) {
      expect(deliverable([...run, step({ step: later, stage: "routed" })])).toBe(true);
    }
    // Even if a later envelope stopped repeating the stage, route having run
    // is still the answer.
    expect(deliverable([...run, step({ step: "case", stage: "cased" })])).toBe(true);
    // But nothing before route may open it.
    expect(deliverable([step({ step: "propose", stage: "proposed" })])).toBe(false);
    expect(
      deliverable([step({ step: "propose", stage: "proposed" }), step({ step: "place", stage: "placed" })])
    ).toBe(false);
  });

  it("reads the review from wherever it sits in the history, not only the end", () => {
    const history = [
      step({ step: "route", stage: "routed" }),
      step({ step: "review", stage: "routed", blockers: ["VIN has no bulk cap", "no fuse"] }),
      step({ step: "sourcing", stage: "routed" }),
      step({ step: "order", stage: "routed" }),
    ];
    expect(reviewState(history)).toEqual({ reviewed: true, blockers: 2, failed: false, failure: null });
  });

  it("a failed review is a third state, and its zero blockers are not a finding", () => {
    const failed = step({
      step: "review",
      stage: "routed",
      findings: [],
      blockers: [],
      review: { status: "failed", ran: true, detail: "the critic answered nothing readable", note: "review failed" },
    });
    const state = reviewState([...ROUTED_PAIR, failed]);
    expect(state.reviewed).toBe(true);
    expect(state.failed).toBe(true);
    expect(state.failure).toBe(
      "review failed: the critic answered nothing readable — nothing is known about this board"
    );
    expect(scheduleNote([...ROUTED_PAIR, failed])).toBe(
      "The review failed: the critic answered nothing readable — nothing is known about this board, so nothing can be booked."
    );
    // The three sentences the calendar row can say, none of them "no blockers" over a failure.
    expect(scheduleNote(ROUTED_PAIR)).toContain("has not run yet");
    expect(scheduleNote([...ROUTED_PAIR, step({ step: "review", stage: "routed", blockers: [] })])).toBe(
      "The review found no blockers, so there is nothing to book."
    );
    expect(scheduleNote([...ROUTED_PAIR, step({ step: "review", stage: "routed", blockers: ["x", "y"] })])).toContain(
      "found 2 blockers"
    );
    // An ok block with no findings is the clean reading, the same as no block.
    const clean = step({
      step: "review",
      stage: "routed",
      blockers: [],
      review: { status: "ok", ran: true, detail: null, note: "" },
    });
    expect(reviewState([clean]).failed).toBe(false);
    // Skipped reads like failed: nothing is known.
    const skipped = step({
      step: "review",
      stage: "routed",
      review: { status: "skipped", ran: false, detail: null, note: "not requested" },
    });
    expect(reviewState([skipped]).failed).toBe(true);
    expect(reviewState([skipped]).failure).toContain("review skipped: not requested");
  });
});

const ROUTED_PAIR = [step({ step: "place", stage: "placed" }), step({ step: "route", stage: "routed" })];

describe("needsGoogleSignIn", () => {
  it("asks for a browser consent only when the OAuth client is set and no token exists", () => {
    expect(needsGoogleSignIn(null)).toBe(false);
    expect(needsGoogleSignIn(READY)).toBe(false);
    expect(
      needsGoogleSignIn({
        available: true,
        chat: true,
        gmail: false,
        calendar: false,
        oauth_client: false,
        signed_in: false,
        token: "missing",
        hints: [],
      })
    ).toBe(false);
    expect(
      needsGoogleSignIn(
        {
          available: true,
          chat: true,
          gmail: false,
          calendar: false,
          oauth_client: false,
          signed_in: false,
          token: "missing",
          hints: [],
        },
        true
      )
    ).toBe(true);
    expect(
      needsGoogleSignIn({
        available: true,
        chat: true,
        gmail: false,
        calendar: false,
        oauth_client: true,
        signed_in: false,
        token: "missing",
        hints: ["sign in with Google from Hardy's Send panel"],
      })
    ).toBe(true);
    // Older engines: no oauth_client flag, only the CLI hint.
    expect(
      needsGoogleSignIn({
        available: true,
        chat: true,
        gmail: false,
        calendar: false,
        token: "missing",
        hints: ["Gmail and Calendar: run `python -m googleapps auth` to sign in"],
      })
    ).toBe(true);
  });
});

describe("hintFor", () => {
  it("is silent while the config is unknown and when the destination is ready", () => {
    expect(hintFor(null, "chat")).toBeNull();
    expect(hintFor(READY, "chat")).toBeNull();
    expect(hintFor(READY, "email")).toBeNull();
    expect(hintFor(READY, "calendar")).toBeNull();
  });

  it("hands over the engine's own hint for the destination that is off", () => {
    const config: DeliverConfig = {
      available: true,
      chat: false,
      gmail: false,
      calendar: false,
      token: "missing",
      hints: [
        "Google Chat: set GOOGLEAPPS_CHAT_WEBHOOK in the service's environment (the service does not read .env)",
        "Gmail and Calendar: sign in with Google from Hardy's Send panel (or run `python -m googleapps auth`; token at /x)",
      ],
    };
    expect(hintFor(config, "chat")).toContain("GOOGLEAPPS_CHAT_WEBHOOK");
    expect(hintFor(config, "chat")).not.toContain("googleapps auth");
    expect(hintFor(config, "email")).toContain("Hardy's Send panel");
    expect(hintFor(config, "calendar")).toContain("python -m googleapps auth");
  });

  it("says so when the engine has no package at all", () => {
    const config: DeliverConfig = {
      available: false,
      chat: false,
      gmail: false,
      calendar: false,
      token: "missing",
      hints: ["the googleapps package is not installed beside the service"],
    };
    expect(hintFor(config, "email")).toContain("not installed");
  });
});

describe("describe*", () => {
  it("reports what was sent, where, and the id", () => {
    expect(describeChat({ session: "s1", chat: { ok: true } })).toBe("Posted the run card to Chat.");
    expect(
      describeEmail({
        session: "s1",
        email: { ok: true, to: ["lead@example.com"], message_id: "msg-1" },
      })
    ).toBe("Sent the board to lead@example.com (message id msg-1).");
    expect(
      describeCalendar({
        session: "s1",
        calendar: { ok: true, blockers: 2, meet_uri: "https://meet.google.com/abc" },
      })
    ).toBe("Booked the review for tomorrow, 2 blockers on the agenda. Meet: https://meet.google.com/abc");
  });

  it("keeps the engine's error text verbatim and never calls a skip a failure", () => {
    expect(
      describeChat({ session: "s1", chat: { ok: false, error: "http_403: the Chat webhook (<set, 80 chars>) rejected the card" } })
    ).toBe("Chat refused it: http_403: the Chat webhook (<set, 80 chars>) rejected the card");
    expect(
      describeCalendar({ session: "s1", calendar: { ok: false, skipped_reason: "no blockers — nothing scheduled" } })
    ).toBe("Nothing booked: no blockers — nothing scheduled.");
    expect(describeCalendar({ session: "s1", calendar: { ok: false, error: "401: token" } })).toBe(
      "Calendar refused it: 401: token"
    );
  });

  it("only describes the destinations the engine reported on", () => {
    expect(describeDelivery({ session: "s1", chat: { ok: true } })).toEqual({
      chat: "Posted the run card to Chat.",
    });
    expect(describeDelivery({ session: "s1" })).toEqual({});
  });
});

const AGENDA: SpecReviewBlock = {
  title: "3.3V rail spec review",
  summary: "Three open questions the board cannot settle on its own.",
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
      refs: [],
    },
  ],
  decisions_needed: ["Pick the input supply"],
  prepared_from: ["review", "route"],
};

describe("spec review", () => {
  it("takes the engine's agenda from the latest step that carried one", () => {
    const history = [
      step({ step: "review", stage: "routed", spec_review: AGENDA }),
      step({ step: "sourcing", stage: "routed" }),
    ];
    expect(specReviewFrom(history)?.title).toBe("3.3V rail spec review");
    expect(specReviewFrom([step({ step: "place", stage: "placed" })])).toBeNull();
  });

  it("finds the agenda after every later step has run, and offers it with no note", () => {
    // The agenda rides the `review` envelope (`service/steps.py:_review`), so
    // by the time the engineer reaches the send panel it is three steps back.
    const history = [
      step({ step: "route", stage: "routed" }),
      step({ step: "review", stage: "routed", spec_review: AGENDA, blockers: ["VIN transient"] }),
      step({ step: "sourcing", stage: "routed" }),
      step({ step: "order", stage: "routed" }),
      step({ step: "case", stage: "routed" }),
    ];
    expect(specReviewFrom(history)?.title).toBe("3.3V rail spec review");
    const offer = specReviewOffer(history);
    expect(offer.review?.title).toBe("3.3V rail spec review");
    expect(offer.note).toBe("");
  });

  it("prefers the engine's own total and only sums when it sent none", () => {
    expect(agendaMinutes(AGENDA)).toBe(20);
    expect(agendaMinutes({ ...AGENDA, total_minutes: 30 })).toBe(30);
  });

  it("separates a review that never ran from one that found nothing to meet about", () => {
    expect(specReviewOffer([step({ step: "place", stage: "placed" })]).note).toBe(
      "The review step has not run, so there is no agenda yet."
    );
    const ran = [step({ step: "review", stage: "routed", blockers: [] })];
    expect(specReviewOffer(ran).note).toContain("proposed no agenda");

    const nothingBlocking: SpecReviewBlock = {
      ...AGENDA,
      items: [{ ...AGENDA.items[1] }],
    };
    const offer = specReviewOffer([
      step({ step: "review", stage: "routed", spec_review: nothingBlocking }),
    ]);
    expect(offer.review).toBe(nothingBlocking);
    expect(offer.note).toBe("No blockers — nothing needs a meeting.");
    expect(blockingItems(AGENDA).map((i) => i.topic)).toEqual(["Input transient rating"]);
    expect(specReviewOffer([step({ step: "review", stage: "routed", spec_review: AGENDA })]).note).toBe("");
  });

  it("refuses to offer a booking over a failed review, even one the engine drafted an agenda for", () => {
    const failed = step({
      step: "review",
      stage: "routed",
      findings: [],
      blockers: [],
      spec_review: AGENDA,
      review: { status: "failed", ran: true, detail: "ModelError: 503", note: "review failed" },
    });
    const offer = specReviewOffer([...ROUTED_PAIR, failed]);
    expect(offer.refused).toBe(true);
    expect(offer.review).toBeNull();
    expect(offer.note).toBe(
      "The review failed: ModelError: 503 — nothing is known about this board, so no spec review can be booked."
    );
    // The other answers are not refusals: the button stays live for the engine to decide.
    expect(specReviewOffer([step({ step: "review", stage: "routed", spec_review: AGENDA })]).refused).toBe(false);
    expect(specReviewOffer([step({ step: "place", stage: "placed" })]).refused).toBe(false);
  });

  it("reports a booking, and a deliberate skip as a result rather than a failure", () => {
    expect(
      describeSpecReview({
        session: "s1",
        spec_review: { ok: true, meet_uri: "https://meet.google.com/abc", html_link: "https://cal/1" },
      })
    ).toBe("Booked the spec review. Meet: https://meet.google.com/abc Invite: https://cal/1");
    expect(
      describeSpecReview({
        session: "s1",
        spec_review: { ok: false, skipped_reason: "the review step has not run" },
      })
    ).toBe("No spec review booked: the review step has not run.");
    expect(
      describeSpecReview({ session: "s1", spec_review: { ok: false, error: "401: token" } })
    ).toBe("The spec review was refused: 401: token");
    expect(describeSpecReview({ session: "s1" })).toBeNull();
    expect(
      describeDelivery({ session: "s1", spec_review: { ok: true } })
    ).toEqual({ spec_review: "Booked the spec review." });
  });
});

describe("summary mode", () => {
  beforeEach(() => window.localStorage.clear());

  it("defaults to prose, remembers a choice, and ignores a value it did not write", () => {
    expect(readSummaryMode()).toBe("prose");
    writeSummaryMode("structured");
    expect(readSummaryMode()).toBe("structured");
    window.localStorage.setItem("kaleo.summaryMode", "interpretive dance");
    expect(readSummaryMode()).toBe("prose");
  });

  it("carries the choice into the request as one foldable object", () => {
    expect(summaryFields("structured")).toEqual({ summary: "structured" });
    expect(summaryFields("prose")).toEqual({ summary: "prose" });
  });
});

// ------------------------------------------------------------ when to meet

import { whenInPast, whenToRfc3339 } from "./deliver";

describe("whenToRfc3339", () => {
  function expectedOffset(moment: Date): string {
    const behind = moment.getTimezoneOffset();
    const sign = behind > 0 ? "-" : "+";
    const hh = String(Math.floor(Math.abs(behind) / 60)).padStart(2, "0");
    const mm = String(Math.abs(behind) % 60).padStart(2, "0");
    return `${sign}${hh}:${mm}`;
  }

  it("empty means no preference: null, the engine's own slot", () => {
    expect(whenToRfc3339("")).toBeNull();
    expect(whenToRfc3339("   ")).toBeNull();
  });

  it("carries the typed wall-clock with this machine's offset for that instant", () => {
    const out = whenToRfc3339("2031-06-08T15:30");
    // The engine's `parse_when` needs an offset; the wall-clock is untouched.
    expect(out).toBe(`2031-06-08T15:30:00${expectedOffset(new Date(2031, 5, 8, 15, 30))}`);
    expect(out).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/);
    // Seconds survive when the control emits them.
    expect(whenToRfc3339("2031-06-08T15:30:45")).toMatch(/T15:30:45[+-]/);
    // Round-trips to the same instant the wall-clock names.
    expect(new Date(out as string).getTime()).toBe(new Date(2031, 5, 8, 15, 30).getTime());
  });

  it("passes anything else through verbatim so the engine's 400 names it, never smoothed to null", () => {
    expect(whenToRfc3339("next tuesday")).toBe("next tuesday");
    expect(whenToRfc3339(" 2031-13-45T99:99 ")).toBe("2031-13-45T99:99");
  });
});

describe("whenInPast", () => {
  const now = new Date(2031, 5, 8, 12, 0, 0);
  it("is a courtesy only: true for a typed time at or before now, false for empty or unreadable", () => {
    expect(whenInPast("2031-06-08T11:59", now)).toBe(true);
    expect(whenInPast("2031-06-08T12:00", now)).toBe(true);
    expect(whenInPast("2031-06-08T12:01", now)).toBe(false);
    expect(whenInPast("", now)).toBe(false);
    expect(whenInPast("next tuesday", now)).toBe(false);
  });
});
