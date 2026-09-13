// @vitest-environment jsdom
//
// The orb, and the rule that outranks how it looks: it must never animate as
// though listening when the microphone is not open.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
// The mouth is polled for "am I talking"; the real one reaches for
// speechSynthesis at construction, which jsdom does not have.
vi.mock("@/lib/speech", () => ({
  speaker: { isSpeaking: () => false, speak: async () => {}, stop: () => {} },
}));

import type { EngineHealth } from "@/hooks";
import { clearMicLevel, publishMicLevel, setPushToTalk } from "@/hooks/useMicLevel";
import { orbHealth, orbIsHot, orbState, orbTitle, VoiceOrb } from "./VoiceOrb";

const base = {
  engineOk: true,
  engineKnown: true,
  micOpen: false,
  recording: false,
  justHeard: false,
  thinking: false,
  speaking: false,
};

const engine = (over: Partial<EngineHealth> = {}): EngineHealth =>
  ({
    ok: true,
    checking: false,
    lastCheckedAt: 1,
    detail: null,
    recheck: vi.fn(),
    ...over,
  }) as unknown as EngineHealth;

afterEach(() => {
  cleanup();
  clearMicLevel();
  setPushToTalk(false);
});

describe("orbState", () => {
  it("cannot be driven by the wake-word switch, because the switch is not an input", () => {
    // The founder's bug in one assertion: the old dot went green from
    // `enabled`. `OrbInput` has no such field, so the only way to reach
    // "listening" is `micOpen` — the listener's own report that the
    // microphone is open. A hypothetical "enabled" is not even expressible.
    expect(Object.keys(base)).not.toContain("enabled");
    expect(orbState({ ...base, micOpen: false })).not.toBe("listening");
    expect(orbState({ ...base, micOpen: true })).toBe("listening");
  });

  it("puts the open microphone above the machine's own work", () => {
    // A hot mic is the one fact the app must never hide, so it outranks both
    // a run in flight and the voice talking back.
    expect(orbState({ ...base, micOpen: true, thinking: true, speaking: true })).toBe(
      "listening"
    );
    expect(orbState({ ...base, thinking: true, speaking: true })).toBe("speaking");
    expect(orbState({ ...base, thinking: true })).toBe("thinking");
  });

  it("puts the finger on the button above everything", () => {
    expect(
      orbState({ ...base, recording: true, micOpen: true, justHeard: true, thinking: true })
    ).toBe("recording");
    expect(orbState({ ...base, justHeard: true, micOpen: true })).toBe("heard");
  });

  it("falls back to the engine dot it replaced when there is nothing else to say", () => {
    expect(orbState(base)).toBe("idle");
    expect(orbState({ ...base, engineOk: false })).toBe("fault");
    expect(orbState({ ...base, engineKnown: false })).toBe("unknown");
  });

  it("keeps engine health beside the voice state, never folded into it", () => {
    const listeningWhileDown = { ...base, micOpen: true, engineOk: false };
    expect(orbState(listeningWhileDown)).toBe("listening");
    expect(orbHealth(listeningWhileDown)).toBe("down");
    expect(orbHealth({ engineOk: true, engineKnown: false })).toBe("unknown");
  });

  it("says something different in every state", () => {
    const states = [
      "fault",
      "unknown",
      "idle",
      "listening",
      "heard",
      "recording",
      "thinking",
      "speaking",
    ] as const;
    const said = states.map((s) => orbTitle(s, "up", "http://127.0.0.1:8081"));
    expect(new Set(said).size).toBe(states.length);
    expect(orbTitle("listening", "down", "http://x", "connection refused")).toContain(
      "connection refused"
    );
  });

  it("knows which states mean the microphone is open", () => {
    expect(orbIsHot("listening")).toBe(true);
    expect(orbIsHot("recording")).toBe(true);
    expect(orbIsHot("heard")).toBe(true);
    expect(orbIsHot("idle")).toBe(false);
    expect(orbIsHot("thinking")).toBe(false);
    expect(orbIsHot("speaking")).toBe(false);
  });
});

describe("VoiceOrb", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearMicLevel();
  });
  afterEach(() => vi.useRealTimers());

  const orb = () => screen.getByTestId("voice-orb");

  it("keeps the engine dot's job: the same handle, and click still re-checks", () => {
    const recheck = vi.fn();
    render(<VoiceOrb engine={engine({ recheck })} baseUrl="http://127.0.0.1:8081" />);
    const button = screen.getByTestId("engine-status");
    expect(button.getAttribute("data-online")).toBe("true");
    fireEvent.click(button);
    expect(recheck).toHaveBeenCalled();
  });

  it("marks an unreachable engine in shape as well as colour", () => {
    render(
      <VoiceOrb engine={engine({ ok: false, detail: "refused" })} baseUrl="http://x" />
    );
    expect(orb().getAttribute("data-state")).toBe("fault");
    expect(orb().getAttribute("data-health")).toBe("down");
    // Two non-colour carriers: a broken ring and a slash through the core.
    expect(screen.getByTestId("orb-health-ring")).toBeTruthy();
    expect(screen.getByTestId("orb-fault-slash")).toBeTruthy();
  });

  it("still shows the engine is down while it is listening", () => {
    render(<VoiceOrb engine={engine({ ok: false })} baseUrl="http://x" micOpen />);
    expect(orb().getAttribute("data-state")).toBe("listening");
    expect(screen.getByTestId("orb-health-ring")).toBeTruthy();
  });

  it("never draws the level ring while the microphone is closed, whatever the bus carries", () => {
    // The honesty rule, rendered: a stray publish (a listener shutting down,
    // a stale frame) must not make a closed microphone look alive.
    render(<VoiceOrb engine={engine()} baseUrl="http://x" micOpen={false} />);
    act(() => {
      publishMicLevel(90, Date.now());
      vi.advanceTimersByTime(120);
    });
    expect(orb().getAttribute("data-state")).toBe("idle");
    expect(orb().getAttribute("data-signal")).toBe("off");
    expect(screen.queryByTestId("orb-amplitude")).toBeNull();
  });

  it("reacts to the real microphone while it is open", () => {
    render(<VoiceOrb engine={engine()} baseUrl="http://x" micOpen />);
    expect(orb().getAttribute("data-signal")).toBe("none");
    expect(screen.queryByTestId("orb-amplitude")).toBeNull();

    act(() => {
      publishMicLevel(44, Date.now());
      vi.advanceTimersByTime(60);
    });
    expect(orb().getAttribute("data-signal")).toBe("live");
    const loud = Number(screen.getByTestId("orb-amplitude").getAttribute("r"));

    act(() => {
      publishMicLevel(4, Date.now());
      vi.advanceTimersByTime(400);
    });
    const quiet = Number(screen.getByTestId("orb-amplitude").getAttribute("r"));
    expect(quiet).toBeLessThan(loud);
  });

  it("degrades to a plain state when the level goes away", () => {
    render(<VoiceOrb engine={engine()} baseUrl="http://x" micOpen />);
    act(() => {
      publishMicLevel(44, Date.now());
      vi.advanceTimersByTime(60);
    });
    expect(screen.queryByTestId("orb-amplitude")).not.toBeNull();
    act(() => {
      clearMicLevel();
      vi.advanceTimersByTime(60);
    });
    // Still listening — that is true and must keep showing — but nothing is
    // measuring, and the orb says "none" rather than drawing a made-up wave.
    expect(orb().getAttribute("data-state")).toBe("listening");
    expect(orb().getAttribute("data-signal")).toBe("none");
    expect(screen.queryByTestId("orb-amplitude")).toBeNull();
  });

  it("holds still when the system asks for less motion", () => {
    render(<VoiceOrb engine={engine()} baseUrl="http://x" micOpen reducedMotion />);
    expect(orb().getAttribute("data-motion")).toBe("still");
    // The state is still legible: it is the motion that goes, not the answer.
    expect(orb().getAttribute("data-state")).toBe("listening");
  });

  it("shows work and speech as their own states", () => {
    const { rerender } = render(
      <VoiceOrb engine={engine()} baseUrl="http://x" thinking />
    );
    expect(orb().getAttribute("data-state")).toBe("thinking");
    expect(screen.getByTestId("orb-arc")).toBeTruthy();
    rerender(
      <VoiceOrb engine={engine()} baseUrl="http://x" isSpeaking={() => true} />
    );
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(orb().getAttribute("data-state")).toBe("speaking");
  });

  it("holds completely still when the microphone is closed", () => {
    // A green core breathing at a shut microphone is one glance away from
    // the lie. `idle` therefore has no animation at all, and the CSS is what
    // this asserts, because there is nothing else to look at.
    render(<VoiceOrb engine={engine()} baseUrl="http://x" />);
    expect(orb().getAttribute("data-state")).toBe("idle");
    const css = orb().querySelector("style")?.textContent ?? "";
    expect(css).not.toMatch(/\[data-state="idle"\][^{]*\{[^}]*animation/);
    expect(css).toMatch(/\[data-state="listening"\][^{]*\{[^}]*animation/);
  });

  it("shows the microphone open while push-to-talk holds it, even though the ear stopped", () => {
    // Holding the mic button calls wake.stop(), so `micOpen` is false for the
    // whole hold. Reading only that field would draw a closed microphone at
    // an open one — the founder's bug inverted.
    render(<VoiceOrb engine={engine()} baseUrl="http://x" micOpen={false} />);
    expect(orb().getAttribute("data-state")).toBe("idle");
    act(() => setPushToTalk(true));
    expect(orb().getAttribute("data-state")).toBe("recording");
    act(() => setPushToTalk(false));
    expect(orb().getAttribute("data-state")).toBe("idle");
  });

  it("shows the wake landing, and push-to-talk, as distinct states", () => {
    const { rerender } = render(
      <VoiceOrb engine={engine()} baseUrl="http://x" micOpen justHeard />
    );
    expect(orb().getAttribute("data-state")).toBe("heard");
    rerender(<VoiceOrb engine={engine()} baseUrl="http://x" recording />);
    expect(orb().getAttribute("data-state")).toBe("recording");
  });
});
