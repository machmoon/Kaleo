// @vitest-environment jsdom
//
// The orb skin's two guarantees, both of which outrank how it looks:
//
//  1. At rest it is an orb and nothing else — no field, no run control.
//  2. Nothing in it may claim the microphone is open when it is not, and the
//     paid control is absent rather than greyed when pressing it would not be
//     valid.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
// The mouth is polled for "am I talking"; the real one reaches for
// speechSynthesis at construction, which jsdom does not have.
vi.mock("@/lib/speech", () => ({
  speaker: { isSpeaking: () => false, speak: async () => {}, stop: () => {} },
}));

import type { EngineHealth } from "@/hooks";
import { clearMicLevel, setPushToTalk } from "@/hooks/useMicLevel";
import { OrbSkin, orbCaption, submittable, type OrbSkinProps } from "./OrbSkin";

const engine = (over: Partial<EngineHealth> = {}): EngineHealth =>
  ({
    baseUrl: "http://127.0.0.1:8081",
    ok: true,
    checking: false,
    lastCheckedAt: 1,
    detail: "",
    recheck: vi.fn(),
    ...over,
  }) as EngineHealth;

function draw(over: Partial<OrbSkinProps> = {}) {
  const props: OrbSkinProps = {
    engine: engine(),
    baseUrl: "http://127.0.0.1:8081",
    value: "",
    onChange: vi.fn(),
    onSubmit: vi.fn(),
    // Every test states motion explicitly: jsdom has no matchMedia, so the
    // default would silently be "animated" and the reduced-motion test would
    // be the only one that ever exercised the other branch.
    reducedMotion: false,
    isSpeaking: () => false,
    ...over,
  };
  return { props, ...render(<OrbSkin {...props} />) };
}

const gate = {
  value: "a 3.3V LDO",
  canStart: true,
  busy: false,
  hidden: false,
  engineOk: true,
  engineKnown: true,
};

afterEach(() => {
  cleanup();
  clearMicLevel();
  setPushToTalk(false);
});

describe("the resting frame", () => {
  it("is an orb and nothing else", () => {
    // The catalogue row, as an assertion: "Collapses to a listening orb; the
    // field appears when you speak or click." An orb plus a field plus a
    // toolbar is the design missed.
    draw();
    expect(screen.getByTestId("orb-skin-orb")).toBeTruthy();
    expect(screen.queryByTestId("orb-skin-field")).toBeNull();
    expect(screen.queryByTestId("orb-skin-input")).toBeNull();
    expect(screen.queryByTestId("orb-submit")).toBeNull();
  });

  it("says in words that a click is what opens the field", () => {
    draw();
    expect(screen.getByTestId("orb-skin-caption").textContent).toBe("Click to type.");
  });

  it("still carries the engine, because the orb took the engine dot's job", () => {
    draw({ engine: engine({ ok: false, lastCheckedAt: 2, detail: "connection refused" }) });
    expect(screen.getByTestId("engine-status").getAttribute("data-online")).toBe("false");
  });
});

describe("how the field appears", () => {
  it("opens on a click on the orb", () => {
    draw();
    fireEvent.click(screen.getByTestId("orb-skin-orb"));
    expect(screen.getByTestId("orb-skin-input")).toBeTruthy();
  });

  it("opens when speech opens the microphone, without anyone clicking", () => {
    draw({ micOpen: true });
    expect(screen.getByTestId("orb-skin-input")).toBeTruthy();
    expect(screen.queryByTestId("orb-skin-caption")).toBeNull();
  });

  it("stays open while there is a draft, so typed text is never hidden by a collapse", () => {
    draw({ value: "a 3.3V LDO" });
    expect(screen.getByTestId("orb-skin-input")).toBeTruthy();
  });

  it("goes back to being an orb on Escape", () => {
    draw();
    fireEvent.click(screen.getByTestId("orb-skin-orb"));
    fireEvent.keyDown(screen.getByTestId("orb-skin-input"), { key: "Escape" });
    expect(screen.queryByTestId("orb-skin-input")).toBeNull();
    expect(screen.getByTestId("orb-skin-orb")).toBeTruthy();
  });
});

describe("the orb may never claim to be listening while the microphone is shut", () => {
  it("draws nothing that moves from the wake-word switch", () => {
    // The bug this whole lane exists to prevent: the old indicator was driven
    // by `wake.enabled`, the switch, so it went green while the mic was still
    // opening, while a permission probe was in flight, and even after the
    // listener refused to arm. The switch is `listening` here and it reaches
    // no moving element at all.
    draw({ listening: true, micOpen: false });
    expect(screen.getByTestId("orb-skin").getAttribute("data-mic")).toBe("closed");
    expect(screen.queryByTestId("orb-skin-glow")).toBeNull();
    expect(screen.getByTestId("voice-orb").getAttribute("data-state")).not.toBe("listening");
  });

  it("says the ear is on and the microphone is not open, in one sentence", () => {
    expect(orbCaption(false, true, false)).toBe("Ear on — the microphone isn’t open yet.");
    expect(orbCaption(true, true, false)).toBe("I’m listening.");
    expect(orbCaption(false, false, false)).toBe("Click to type.");
    expect(orbCaption(false, false, true)).toBe("Working on it.");
  });

  it("ignores the amplitude level entirely when the microphone is closed", () => {
    // A meter that keeps waving after the mic shuts is the same lie in a
    // slower form, and `level` is the one prop that could carry it in.
    draw({ micOpen: false, level: 0.9 });
    expect(screen.queryByTestId("orb-skin-glow")).toBeNull();
  });

  it("draws the glow only when the microphone reports itself open", () => {
    draw({ micOpen: true, level: 0.5 });
    const glow = screen.getByTestId("orb-skin-glow");
    expect(glow.getAttribute("data-level")).toBe("0.50");
    expect(screen.getByTestId("voice-orb").getAttribute("data-state")).toBe("listening");
  });

  it("clamps a level from outside rather than trusting it", () => {
    draw({ micOpen: true, level: 12 });
    expect(screen.getByTestId("orb-skin-glow").getAttribute("data-level")).toBe("1.00");
  });
});

describe("motion", () => {
  it("stops animating when the OS asks for less motion", () => {
    // An always-on-top overlay that animates forever is a battery and an
    // attention cost. None of the OSS orbs the research pass opened honour
    // this; this one renders one static frame and stops.
    draw({ micOpen: true, level: 0.4, reducedMotion: true });
    const skin = screen.getByTestId("orb-skin");
    expect(skin.getAttribute("data-motion")).toBe("still");
    expect(screen.getByTestId("orb-skin-glow").className).not.toContain("kv-orbskin-pulse");
    expect(screen.getByTestId("voice-orb").getAttribute("data-motion")).toBe("still");
  });

  it("animates the glow only while the microphone is open", () => {
    draw({ micOpen: true, level: 0.4, reducedMotion: false });
    expect(screen.getByTestId("orb-skin-glow").className).toContain("kv-orbskin-pulse");
  });
});

describe("the control that spends money", () => {
  it("does not exist with nothing typed", () => {
    expect(submittable({ ...gate, value: "   " })).toBe(false);
    draw({ open: true, value: "" });
    expect(screen.queryByTestId("orb-submit")).toBeNull();
  });

  it("does not exist before the first engine probe has landed", () => {
    // Three engine states, not two: before a probe nobody knows whether a run
    // would reach anything, and offering to spend on a guess flattens the
    // unknown state into "up".
    expect(submittable({ ...gate, engineKnown: false })).toBe(false);
    draw({ open: true, value: "a 3.3V LDO", engine: engine({ lastCheckedAt: null }) });
    expect(screen.queryByTestId("orb-submit")).toBeNull();
  });

  it("does not exist while the engine is unreachable", () => {
    expect(submittable({ ...gate, engineOk: false })).toBe(false);
    draw({ open: true, value: "a 3.3V LDO", engine: engine({ ok: false }) });
    expect(screen.queryByTestId("orb-submit")).toBeNull();
  });

  it("does not exist while a run is already in flight, so one prompt cannot fire twice", () => {
    expect(submittable({ ...gate, busy: true })).toBe(false);
    draw({ open: true, value: "a 3.3V LDO", busy: true });
    expect(screen.queryByTestId("orb-submit")).toBeNull();
  });

  it("does not exist while the overlay is hidden or the run guard is closed", () => {
    expect(submittable({ ...gate, hidden: true })).toBe(false);
    expect(submittable({ ...gate, canStart: false })).toBe(false);
  });

  it("is absent rather than greyed — no disabled run control is ever rendered", () => {
    // "Absent, not greyed" is the rule; a disabled button is an invitation
    // with the reason hidden inside an attribute nobody can read.
    draw({ open: true, value: "a 3.3V LDO", engine: engine({ ok: false }) });
    for (const button of screen.queryAllByRole("button")) {
      expect(button.getAttribute("data-testid")).not.toBe("orb-submit");
    }
    expect(document.querySelector("[data-testid='orb-submit'][disabled]")).toBeNull();
  });

  it("appears, states its price, and is the only path to a run", () => {
    const { props } = draw({ open: true, value: "a 3.3V LDO" });
    const submit = screen.getByTestId("orb-submit");
    expect(submit.getAttribute("title")).toContain("costs money");
    fireEvent.click(submit);
    expect(props.onSubmit).toHaveBeenCalledTimes(1);
  });

  it("lets Enter start a run only when the same gate is open", () => {
    const { props } = draw({ open: true, value: "a 3.3V LDO", engine: engine({ ok: false }) });
    fireEvent.keyDown(screen.getByTestId("orb-skin-input"), { key: "Enter" });
    expect(props.onSubmit).not.toHaveBeenCalled();
    cleanup();

    const ok = draw({ open: true, value: "a 3.3V LDO" });
    fireEvent.keyDown(screen.getByTestId("orb-skin-input"), { key: "Enter" });
    expect(ok.props.onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe("the field's own controls", () => {
  it("offers a way to stop listening only while the microphone is open", () => {
    const onStopListening = vi.fn();
    draw({ open: true, micOpen: false, onStopListening });
    expect(screen.queryByTestId("orb-skin-stop")).toBeNull();
    cleanup();

    draw({ open: true, micOpen: true, onStopListening });
    fireEvent.click(screen.getByTestId("orb-skin-stop"));
    expect(onStopListening).toHaveBeenCalledTimes(1);
  });

  it("hands typing straight back to the caller, which owns the draft", () => {
    const { props } = draw({ open: true, value: "a" });
    fireEvent.change(screen.getByTestId("orb-skin-input"), { target: { value: "ab" } });
    expect(props.onChange).toHaveBeenCalledWith("ab");
  });
});
