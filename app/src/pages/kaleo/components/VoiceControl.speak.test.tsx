// @vitest-environment jsdom
//
// The TTS half of the one voice control: the switch that stops it TALKING,
// which is not the button that stops it HEARING. Its own file because
// VoiceControl.test.tsx is edited by two other lanes concurrently.
//
// Nothing here makes a sound: the speaker is only ever asked whether it was
// stopped, and the founder is sitting next to this machine.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

const voice = {
  status: "idle" as string,
  transcript: "",
  error: "",
  elapsedS: 0,
  start: vi.fn(async () => {}),
  stop: vi.fn(),
  cancel: vi.fn(),
};
vi.mock("@/hooks/useVoiceInput", () => ({ useVoiceInput: () => voice }));

import type { WakeWord } from "@/hooks/useWakeWord";
import { isVoiceEnabled } from "@/lib/speech";
import { micHasFloor, resetMicFloor } from "@/lib/speech/announce";
import { HOLD_MS, VoiceControl } from "./VoiceControl";

function wakeWord(patch: Partial<WakeWord> = {}): WakeWord {
  return {
    enabled: false,
    setEnabled: vi.fn(),
    listening: false,
    lastHeard: null,
    lastGlimpse: null,
    justHeard: false,
    error: null,
    detail: "",
    backend: null,
    sent: 0,
    cap: 15,
    start: vi.fn(),
    stop: vi.fn(),
    debugTrigger: vi.fn(),
    ...patch,
  } as WakeWord;
}

function draw(props: Partial<React.ComponentProps<typeof VoiceControl>> = {}) {
  return render(
    <VoiceControl
      baseUrl="http://127.0.0.1:8081"
      onTranscript={() => {}}
      wake={wakeWord()}
      {...props}
    />
  );
}

function openMenu() {
  fireEvent.contextMenu(screen.getByTestId("voice-control"));
}

beforeEach(() => {
  window.localStorage.clear();
  resetMicFloor();
  vi.useRealTimers();
});

afterEach(() => {
  cleanup();
  voice.status = "idle";
  vi.clearAllMocks();
});

describe("the voice switch in the menu", () => {
  it("starts on, because talking back is the default", () => {
    draw();
    openMenu();
    const toggle = screen.getByTestId("voice-speak-toggle");
    expect(toggle.getAttribute("data-enabled")).toBe("1");
    expect(screen.getByTestId("voice-speak-state").textContent).toContain("I speak");
  });

  it("silences the voice and remembers it, without touching the microphone", () => {
    const wake = wakeWord({ enabled: true, listening: true });
    draw({ wake });
    openMenu();
    fireEvent.click(screen.getByTestId("voice-speak-toggle"));
    expect(isVoiceEnabled()).toBe(false);
    expect(screen.getByTestId("voice-speak-toggle").getAttribute("data-enabled")).toBe("0");
    expect(screen.getByTestId("voice-speak-state").textContent).toContain("Silenced");
    // The other switch is untouched: one stops it hearing, this stops it
    // talking, and a control that did both would leave no way to ask for one.
    expect(wake.setEnabled).not.toHaveBeenCalled();
    expect(wake.stop).not.toHaveBeenCalled();
  });

  it("reads back as silenced on the next mount — a choice, not a session flag", () => {
    draw();
    openMenu();
    fireEvent.click(screen.getByTestId("voice-speak-toggle"));
    cleanup();
    draw();
    openMenu();
    expect(screen.getByTestId("voice-speak-toggle").getAttribute("data-enabled")).toBe("0");
  });

  it("says which of the two switches it is, in words, in both states", () => {
    draw();
    openMenu();
    const toggle = screen.getByTestId("voice-speak-toggle");
    expect(toggle.getAttribute("title")).toMatch(/microphone is unaffected/i);
    fireEvent.click(toggle);
    expect(screen.getByTestId("voice-speak-toggle").getAttribute("title")).toMatch(
      /I still hear you/i
    );
  });
});

describe("push-to-talk and the floor", () => {
  it("takes the floor while held, so nothing is said into a live recording", () => {
    vi.useFakeTimers();
    draw();
    const button = screen.getByTestId("voice-start");
    fireEvent.pointerDown(button);
    expect(micHasFloor()).toBe(false);
    vi.advanceTimersByTime(HOLD_MS + 10);
    expect(micHasFloor()).toBe(true);
    fireEvent.pointerUp(button);
    expect(micHasFloor()).toBe(false);
    vi.useRealTimers();
  });

  it("gives the floor back after a press that never became a hold", () => {
    vi.useFakeTimers();
    draw();
    const button = screen.getByTestId("voice-start");
    fireEvent.pointerDown(button);
    fireEvent.pointerUp(button);
    fireEvent.pointerLeave(button);
    expect(micHasFloor()).toBe(false);
    vi.useRealTimers();
  });
});
