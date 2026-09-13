// @vitest-environment jsdom
//
// The ear's lifecycle with a scripted listener injected through the factory
// seam. What this file pins: the switch is off by default, one detector at a
// time, a windows-backend wake is arm-once (no re-arm), and the cap or a
// failure switches the ear off rather than trying again on its own.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/capture-permission", () => ({
  ensureCaptureAccess: vi.fn(async () => "granted" as const),
  ensureMicrophoneAccess: vi.fn(async () => "granted" as const),
}));

import { STORAGE_KEYS } from "@/config";
import { CUSTOMIZABLE_VERSION } from "@/lib/storage/customizable.storage";
import type {
  WakeBackendName,
  WakeListenerEvents,
  WakeWordListener,
  createWakeWordListener,
} from "@/lib/wake-word";
import { CONTINUATION_MS } from "@/lib/wake-word";
import { HEARD_FLASH_MS, useWakeWord } from "./useWakeWord";
import { ensureCaptureAccess } from "@/lib/capture-permission";

const mockEnsureMic = vi.mocked(ensureCaptureAccess);

class FakeListener implements WakeWordListener {
  static instances: FakeListener[] = [];
  backend: WakeBackendName = "windows";
  started = 0;
  stopped = 0;
  constructor(public events: WakeListenerEvents) {
    FakeListener.instances.push(this);
  }
  async start() {
    this.started += 1;
    this.events.onState("listening", "listening for the wake word");
  }
  stop() {
    this.stopped += 1;
    this.events.onState("stopped", "stopped listening");
  }
  /** What the real listener does on a wake: stops itself, then reports. */
  hear(utterance: string, gated?: boolean) {
    this.events.onState("stopped", "heard the wake word");
    this.events.onWake({ utterance, backend: this.backend, gated });
  }
}

const create: typeof createWakeWordListener = (_options, events) => new FakeListener(events);

function stored(): { wakeWord?: { isEnabled: boolean } } {
  return JSON.parse(localStorage.getItem(STORAGE_KEYS.CUSTOMIZABLE) ?? "{}");
}

function enableWake() {
  localStorage.setItem(
    STORAGE_KEYS.CUSTOMIZABLE,
    JSON.stringify({ version: CUSTOMIZABLE_VERSION, wakeWord: { isEnabled: true } })
  );
}

beforeEach(() => {
  FakeListener.instances = [];
  localStorage.clear();
  mockEnsureMic.mockReset();
  mockEnsureMic.mockResolvedValue("granted");
});

afterEach(() => {
  vi.useRealTimers();
});

const options = { baseUrl: "http://engine", token: "tok" };

describe("useWakeWord", () => {
  it("is off by default and does not open the microphone until the ear is clicked", async () => {
    const onWake = vi.fn();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake, create }));
    expect(result.current.enabled).toBe(false);
    expect(result.current.listening).toBe(false);
    expect(mockEnsureMic).not.toHaveBeenCalled();
    expect(FakeListener.instances).toHaveLength(0);

    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.listening).toBe(true));
    expect(mockEnsureMic).toHaveBeenCalled();
    expect(FakeListener.instances).toHaveLength(1);
    expect(result.current.backend).toBe("windows");
    expect(stored().wakeWord?.isEnabled).toBe(true);

    act(() => result.current.setEnabled(false));
    expect(FakeListener.instances[0].stopped).toBe(1);
    expect(result.current.listening).toBe(false);
    expect(stored().wakeWord?.isEnabled).toBe(false);
  });

  it("a stale v3 store with wake on does not auto-arm", async () => {
    localStorage.setItem(
      STORAGE_KEYS.CUSTOMIZABLE,
      JSON.stringify({ version: 3, wakeWord: { isEnabled: true } })
    );
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    expect(result.current.enabled).toBe(false);
    expect(FakeListener.instances).toHaveLength(0);
    expect(mockEnsureMic).not.toHaveBeenCalled();
  });

  it("asks for microphone permission and turns off when it is refused", async () => {
    mockEnsureMic.mockRejectedValue(
      new Error(
        "Microphone access is off for Ada. Enable it in System Settings → Privacy & Security → Microphone, then click the ear again."
      )
    );
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.enabled).toBe(false));
    expect(result.current.error).toMatch(/Microphone access is off/);
    expect(FakeListener.instances).toHaveLength(0);
  });

  it("follows a Settings flip from another window via the storage event", async () => {
    enableWake();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    await waitFor(() => expect(result.current.listening).toBe(true));

    act(() => {
      localStorage.setItem(
        STORAGE_KEYS.CUSTOMIZABLE,
        JSON.stringify({ version: CUSTOMIZABLE_VERSION, wakeWord: { isEnabled: false } })
      );
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: STORAGE_KEYS.CUSTOMIZABLE,
          newValue: localStorage.getItem(STORAGE_KEYS.CUSTOMIZABLE),
        })
      );
    });
    expect(result.current.enabled).toBe(false);
    expect(result.current.listening).toBe(false);
  });

  it("turns back on after a disable — the toggle is not one-way", async () => {
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.listening).toBe(true));
    act(() => result.current.setEnabled(false));
    expect(result.current.enabled).toBe(false);
    expect(result.current.listening).toBe(false);

    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.listening).toBe(true));
    expect(result.current.enabled).toBe(true);
    expect(FakeListener.instances.length).toBeGreaterThanOrEqual(2);
    expect(stored().wakeWord?.isEnabled).toBe(true);
  });

  it("after the cap, a click arms a fresh listener again", async () => {
    enableWake();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    const first = FakeListener.instances[0];
    act(() => {
      first.events.onWindow?.(1, 1);
      first.events.onState("capped", "stopped after 1 clip — click the ear to listen again");
    });
    expect(result.current.enabled).toBe(false);

    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(result.current.enabled).toBe(true);
    expect(result.current.listening).toBe(true);
  });

  it("remembers the switch across mounts", async () => {
    enableWake();
    const { result, unmount } = renderHook(() =>
      useWakeWord({ ...options, onWake: vi.fn(), create })
    );
    expect(result.current.enabled).toBe(true);
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    unmount();
    expect(FakeListener.instances[0].stopped).toBe(1);
  });

  it("never runs two detectors", async () => {
    enableWake();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    act(() => result.current.start());
    act(() => result.current.start());
    expect(FakeListener.instances).toHaveLength(1);
    expect(FakeListener.instances[0].started).toBe(1);
  });

  it("hands the utterance to the page, shows it briefly, and stays armed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    enableWake();
    const onWake = vi.fn();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake, create }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));

    act(() => FakeListener.instances[0].hear("make me a 3.3 V LDO board", true));
    await waitFor(() =>
      expect(onWake).toHaveBeenCalledExactlyOnceWith("make me a 3.3 V LDO board")
    );
    expect(result.current.lastHeard).toBe("make me a 3.3 V LDO board");
    expect(result.current.justHeard).toBe(true);
    // A wake word that stops after one hit is a button. The budget, not the
    // hit, is what ends the listening.
    expect(result.current.enabled).toBe(true);
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEARD_FLASH_MS);
    });
    expect(result.current.justHeard).toBe(false);
    expect(result.current.lastHeard).toBe("make me a 3.3 V LDO board");
    vi.useRealTimers();
  });

  it("keeps one budget across re-armings, and stops when it is spent", async () => {
    enableWake();
    const { result } = renderHook(() =>
      useWakeWord({ ...options, onWake: vi.fn(), create, budget: 3 })
    );
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    expect(result.current.cap).toBe(3);
    expect(result.current.remaining).toBe(3);

    act(() => FakeListener.instances[0].events.onWindow?.(1, 3));
    expect(result.current.sent).toBe(1);
    expect(result.current.remaining).toBe(2);

    // A wake re-arms; the count must not reset behind the user's back.
    act(() => FakeListener.instances[0].hear("make me an LDO", true));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(result.current.sent).toBe(1);

    act(() => {
      FakeListener.instances[1].events.onWindow?.(3, 3);
      FakeListener.instances[1].events.onState(
        "capped",
        "that was the last of my 3 listening calls — unmute me again for another 3"
      );
    });
    expect(result.current.budgetSpent).toBe(true);
    expect(result.current.remaining).toBe(0);
    expect(result.current.enabled).toBe(false);
    expect(result.current.listening).toBe(false);
    // It says what happened rather than going quiet.
    expect(result.current.detail).toContain("listening calls");

    // Asking again is a decision, and it gives a whole budget back.
    act(() => result.current.listenAgain());
    await waitFor(() => expect(FakeListener.instances).toHaveLength(3));
    expect(result.current.sent).toBe(0);
    expect(result.current.remaining).toBe(3);
    expect(result.current.budgetSpent).toBe(false);
  });

  it("only inside a follow-up window is speech without the name the command", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    enableWake();
    const seen: Array<() => boolean> = [];
    const spy: typeof createWakeWordListener = (opts, events) => {
      seen.push(opts.continuing ?? (() => false));
      return new FakeListener(events);
    };
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create: spy }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    // Nothing has been said yet: "Ada" is required.
    expect(seen[0]()).toBe(false);

    // The name alone, from a gated window: the rest is still coming.
    act(() => FakeListener.instances[0].hear("", true));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(seen[1]()).toBe(true);

    // Nothing is said. The window closes itself and says so, instead of
    // leaving an ear that answers to any sentence in the room.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTINUATION_MS + 10);
    });
    expect(seen[1]()).toBe(false);
    expect(result.current.detail).toMatch(/Hey Ada/);
    vi.useRealTimers();
  });

  it("one breath — name and command in the same clip — opens no follow-up window", async () => {
    enableWake();
    const seen: Array<() => boolean> = [];
    const spy: typeof createWakeWordListener = (opts, events) => {
      seen.push(opts.continuing ?? (() => false));
      return new FakeListener(events);
    };
    const onWake = vi.fn();
    renderHook(() => useWakeWord({ ...options, onWake, create: spy }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    // matchWakeWord already handed back the tail of the same clip, so the
    // command is used rather than listened for a second time.
    act(() => FakeListener.instances[0].hear("make me a 3.3 V LDO board", true));
    expect(onWake).toHaveBeenCalledExactlyOnceWith("make me a 3.3 V LDO board");
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(seen[1]()).toBe(false);
  });

  it("does not listen while the overlay is hidden, and resumes when it shows", async () => {
    enableWake();
    let hidden = false;
    const { result, rerender } = renderHook(() =>
      useWakeWord({ ...options, hidden, onWake: vi.fn(), create })
    );
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    hidden = true;
    rerender();
    expect(FakeListener.instances[0].stopped).toBe(1);
    expect(result.current.listening).toBe(false);
    expect(result.current.enabled).toBe(true);
    hidden = false;
    rerender();
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
  });

  it("the cap switches the ear off without stopping the window that was paid for", async () => {
    enableWake();
    const { result } = renderHook(() => useWakeWord({ ...options, onWake: vi.fn(), create }));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    const first = FakeListener.instances[0];
    act(() => {
      first.events.onWindow?.(1, 1);
      first.events.onState("capped", "stopped after 1 clip — click the ear to listen again");
    });
    expect(result.current.enabled).toBe(false);
    expect(result.current.listening).toBe(false);
    expect(result.current.sent).toBe(1);
    expect(result.current.detail).toContain("1 clip");
    expect(first.stopped).toBe(0);
    expect(FakeListener.instances).toHaveLength(1);
  });

  it("a refused microphone is an error, and the switch goes off", async () => {
    const refusing: typeof createWakeWordListener = (_o, events) => {
      const listener = new FakeListener(events);
      listener.start = async () => {
        throw new Error("could not use the microphone: Permission denied");
      };
      return listener;
    };
    const { result } = renderHook(() =>
      useWakeWord({ ...options, onWake: vi.fn(), create: refusing })
    );
    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.enabled).toBe(false));
    expect(result.current.error).toMatch(/Permission denied/);
    expect(result.current.listening).toBe(false);
  });

  it("a webview with no recorder says so instead of pretending to listen", async () => {
    const none: typeof createWakeWordListener = () => null;
    const { result } = renderHook(() =>
      useWakeWord({ ...options, onWake: vi.fn(), create: none })
    );
    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(result.current.enabled).toBe(false));
    expect(result.current.error).toContain("no speech recognition");
  });

  it("the free OS recognizer may re-arm after a wake", async () => {
    enableWake();
    const speechCreate: typeof createWakeWordListener = (_options, events) => {
      const listener = new FakeListener(events);
      listener.backend = "speech";
      return listener;
    };
    const onWake = vi.fn();
    const { result } = renderHook(() =>
      useWakeWord({ ...options, onWake, create: speechCreate })
    );
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));
    act(() => FakeListener.instances[0].hear("make me a board"));
    await waitFor(() => expect(onWake).toHaveBeenCalledExactlyOnceWith("make me a board"));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(result.current.enabled).toBe(true);
    expect(result.current.listening).toBe(true);
  });

  it("a local spotter delivers the utterance and never starts Gemini windows", async () => {
    enableWake();
    let deliver: (hit: { utterance?: string }) => void = () => {};
    const subscribeLocal = vi.fn(async (onHit: typeof deliver) => {
      deliver = onHit;
      return vi.fn();
    });
    const ready = {
      available: true,
      listening: false,
      backend: "porcupine",
      reason: "ready",
    };
    const probeLocal = vi.fn(async () => ready);
    const startLocal = vi.fn(async () => ({ ...ready, listening: true }));
    const stopLocal = vi.fn(async () => ready);
    const onWake = vi.fn();
    const { result } = renderHook(() =>
      useWakeWord({
        ...options,
        onWake,
        subscribeLocal,
        probeLocal,
        startLocal,
        stopLocal,
        // Default factory so the hook treats this as production and prefers local.
      })
    );
    await waitFor(() => expect(result.current.backend).toBe("local"));
    expect(result.current.listening).toBe(true);
    expect(startLocal).toHaveBeenCalled();
    expect(mockEnsureMic).not.toHaveBeenCalled();
    act(() => deliver({ utterance: "make me a 3.3 V LDO board" }));
    expect(onWake).toHaveBeenCalledExactlyOnceWith("make me a 3.3 V LDO board");
    expect(result.current.lastHeard).toBe("make me a 3.3 V LDO board");
    expect(subscribeLocal).toHaveBeenCalled();
    expect(stopLocal).toHaveBeenCalled();
  });

  it("preferLocal off ignores an available spotter and takes the paid path", async () => {
    // `wake_status` answers `available` for a keyword *file*, so a failed
    // training run still turns the ear green and then never fires. Until it
    // can tell a working model from a file, this is the way past it.
    enableWake();
    const ready = {
      available: true,
      listening: false,
      backend: "porcupine",
      reason: "ready",
    };
    const probeLocal = vi.fn(async () => ready);
    const startLocal = vi.fn(async () => ({ ...ready, listening: true }));
    const { result } = renderHook(() =>
      useWakeWord({
        ...options,
        onWake: vi.fn(),
        create,
        probeLocal,
        startLocal,
        preferLocal: false,
      })
    );
    await waitFor(() => expect(result.current.backend).toBe("windows"));
    expect(probeLocal).not.toHaveBeenCalled();
    expect(startLocal).not.toHaveBeenCalled();
    expect(result.current.listening).toBe(true);
  });

  it("an empty local hit does not route yet — it opens one command clip", async () => {
    enableWake();
    let deliver: (hit: { utterance?: string }) => void = () => {};
    const subscribeLocal = vi.fn(async (onHit: typeof deliver) => {
      deliver = onHit;
      return vi.fn();
    });
    const ready = {
      available: true,
      listening: false,
      backend: "mock",
      reason: "mock",
    };
    const create = vi.fn((_opts: unknown, events: WakeListenerEvents) => new FakeListener(events));
    const onWake = vi.fn();
    const { result } = renderHook(() =>
      useWakeWord({
        ...options,
        onWake,
        create: create as typeof createWakeWordListener,
        subscribeLocal,
        probeLocal: async () => ready,
        startLocal: async () => ({ ...ready, listening: true }),
        stopLocal: async () => ready,
      })
    );
    await waitFor(() => expect(result.current.backend).toBe("local"));
    act(() => deliver({ utterance: "" }));
    expect(onWake).not.toHaveBeenCalled();
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(mockEnsureMic).toHaveBeenCalled();
  });

  it("a bare name from a gated clip re-arms, and one from an ungated clip does not", async () => {
    const { result } = renderHook(() =>
      useWakeWord({ ...options, onWake: vi.fn(), create, idleGeminiWake: true })
    );
    act(() => result.current.setEnabled(true));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(1));

    // Measured speech, no command yet: keep the ear open for the rest.
    act(() => FakeListener.instances[0].hear("", true));
    await waitFor(() => expect(FakeListener.instances).toHaveLength(2));
    expect(result.current.enabled).toBe(true);

    // The same transcript from a window no gate could confirm is exactly what
    // silence produces against the Ada-primed prompt: spend nothing more.
    act(() => FakeListener.instances[1].hear("", false));
    await waitFor(() => expect(result.current.enabled).toBe(false));
    expect(FakeListener.instances).toHaveLength(2);
  });
});
