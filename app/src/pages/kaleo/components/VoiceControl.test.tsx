// @vitest-environment jsdom
//
// One voice control, and the founder's model for it: a mute button for
// "Hey Ada". The tests pin the three things it must never stop saying —
// unmuted costs money and is capped, a refusal is not the same as being
// muted, and press-and-hold still records, because that is the path that
// actually works.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
import { speaker } from "@/lib/speech";
import { HOLD_MS, VoiceControl, glimpseLine, micState, micTitle, micIsOpen } from "./VoiceControl";

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

afterEach(() => {
  cleanup();
  voice.status = "idle";
  vi.clearAllMocks();
});

describe("micState", () => {
  it("keeps refused apart from muted, because they are different sentences", () => {
    expect(micState(wakeWord(), false)).toBe("muted");
    expect(micState(wakeWord({ enabled: true, listening: true }), false)).toBe("unmuted");
    expect(micState(wakeWord({ error: "I can’t measure the room level" }), false)).toBe(
      "refused"
    );
    expect(micState(wakeWord({ justHeard: true }), false)).toBe("heard");
    expect(micState(wakeWord(), true)).toBe("recording");
    expect(micState(null, false)).toBe("muted");
  });

  it("does not call the switch being on 'listening' — the microphone has to be open", () => {
    // The founder's bug: green came from `enabled`, which is the switch, and
    // the mic was still opening (or a probe was in flight) behind it.
    expect(micState(wakeWord({ enabled: true, listening: false }), false)).toBe("arming");
  });

  it("a spent budget is its own state, not a silent return to muted", () => {
    // The listener stops itself at the cap and turns the switch off with no
    // error at all, so without this the two are the same pixels.
    const capped = wakeWord({
      enabled: false,
      sent: 1,
      cap: 1,
      detail: "stopped after 1 clips — click the ear to listen again",
    });
    expect(micState(capped, false)).toBe("spent");
    // A deliberate mute says something else, and stays muted.
    expect(micState({ ...capped, detail: "stopped listening" }, false)).toBe("muted");
    // With patch A landed the listener says why, and that wins over the sniff.
    expect(micState({ ...capped, stoppedReason: "user" }, false)).toBe("muted");
    expect(
      micState({ ...capped, detail: "", sent: 0, stoppedReason: "cap" }, false)
    ).toBe("spent");
  });
});

describe("glimpseLine", () => {
  it("turns a paid window that was not my name into one quiet sentence", () => {
    expect(glimpseLine("what's the current on that rail")).toBe(
      "I heard “what's the current on that rail” — that was not my name."
    );
    expect(glimpseLine("(no audio in that window)")).toBe(
      "I heard the room, but no words in it."
    );
    expect(glimpseLine("engine: timed out (1/3)")).toBe(
      "The engine couldn’t take that clip — timed out (1/3)"
    );
    expect(glimpseLine(null)).toBeNull();
    expect(glimpseLine("   ")).toBeNull();
  });
});

describe("micTitle", () => {
  it("gives every state its own sentence, all of them in the first person", () => {
    const wake = wakeWord({ cap: 1 });
    const states = ["muted", "arming", "unmuted", "heard", "spent"] as const;
    const said = states.map((s) => micTitle(s, wake, null));
    expect(new Set(said).size).toBe(states.length);
    for (const line of said) expect(line.startsWith("I ")).toBe(true);
    expect(micTitle("arming", wake, null)).toContain("I am not listening yet");
    expect(micTitle("spent", wake, null)).toContain("did not hear my name");
    expect(micTitle("spent", wake, null)).toContain("one four-second window");
  });
});

describe("VoiceControl", () => {
  it("is one control, and it starts muted", () => {
    draw();
    expect(screen.getAllByTestId("voice-start").length).toBe(1);
    expect(screen.queryByTestId("wake-arm")).toBeNull();
    expect(screen.getByTestId("voice-control").getAttribute("data-state")).toBe("muted");
    expect(screen.getByTestId("voice-start").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("voice-start").getAttribute("title")).toContain("I am muted");
  });

  it("a tap unmutes and mutes, and nothing else on it starts a run", () => {
    const wake = wakeWord();
    draw({ wake });
    fireEvent.click(screen.getByTestId("voice-start"));
    expect(wake.setEnabled).toHaveBeenCalledExactlyOnceWith(true);
    expect(screen.queryByTestId("prompt-submit")).toBeNull();
  });

  // "why out of 10" — the counter is off the face of the control. It was a
  // meter nobody asked for. What may not happen is the cost going with it:
  // it moves to the tooltip and the menu, which is where a number gets asked
  // for rather than glanced at.
  it("does not print the budget on the strip", () => {
    draw({ wake: wakeWord({ enabled: true, listening: true, backend: "windows", sent: 3 }) });
    expect(screen.getByTestId("voice-control").getAttribute("data-state")).toBe("unmuted");
    expect(screen.queryByTestId("voice-budget")).toBeNull();
    expect(screen.getByTestId("voice-control").textContent).not.toContain("3 / 15");
  });

  it("still says what it has spent, in the tooltip and in the menu", () => {
    draw({ wake: wakeWord({ enabled: true, listening: true, backend: "windows", sent: 3 }) });
    expect(screen.getByTestId("voice-start").getAttribute("title")).toContain(
      "3 / 15 paid windows used this listen"
    );
    fireEvent.contextMenu(screen.getByTestId("voice-control"));
    const line = screen.getByTestId("voice-menu-budget");
    expect(line.textContent).toBe(
      "3 of 15 paid windows used this listen, one model call each."
    );
    expect(line.getAttribute("data-sent")).toBe("3");
    expect(line.getAttribute("data-cap")).toBe("15");
  });

  it("a free recognizer bills nothing, so there is no cost line at all", () => {
    draw({ wake: wakeWord({ enabled: true, listening: true, backend: "speech" }) });
    expect(screen.queryByTestId("voice-budget")).toBeNull();
    fireEvent.contextMenu(screen.getByTestId("voice-control"));
    expect(screen.queryByTestId("voice-menu-budget")).toBeNull();
  });

  it("says the room level cannot be gated instead of looking like it is listening", () => {
    const refusal =
      "I can’t measure the room level in this webview, so I can’t tell speech from silence — I won’t send clips I can’t gate. Use the mic button instead.";
    draw({ wake: wakeWord({ enabled: false, error: refusal }) });
    expect(screen.getByTestId("voice-control").getAttribute("data-state")).toBe("refused");
    expect(screen.getByTestId("voice-refused").textContent).toBe(refusal);
    expect(screen.getByTestId("voice-start").getAttribute("title")).toContain(refusal);
  });

  it("says it is opening the microphone instead of showing the listening colour", () => {
    draw({ wake: wakeWord({ enabled: true, listening: false, backend: "windows" }) });
    expect(screen.getByTestId("voice-control").getAttribute("data-state")).toBe("arming");
    expect(screen.getByTestId("voice-start").getAttribute("title")).toContain(
      "I am not listening yet"
    );
  });

  it("shows the window it paid for that was not my name, so listening and deaf differ", () => {
    draw({
      wake: wakeWord({
        enabled: true,
        listening: true,
        backend: "windows",
        sent: 1,
        cap: 1,
        lastGlimpse: "so then I said we should just buy the eval board",
      }),
    });
    expect(screen.getByTestId("voice-glimpse").textContent).toBe(
      "I heard “so then I said we should just buy the eval board” — that was not my name."
    );
  });

  it("a spent budget says so on the strip instead of going quietly grey", () => {
    draw({
      wake: wakeWord({
        enabled: false,
        listening: false,
        backend: "windows",
        sent: 1,
        cap: 1,
        detail: "stopped after 1 clips — click the ear to listen again",
      }),
    });
    expect(screen.getByTestId("voice-control").getAttribute("data-state")).toBe("spent");
    expect(screen.getByTestId("voice-spent").textContent).toBe(
      "I stopped after 1 window — click to listen again"
    );
    // The count is off the face now, but the reason it stopped is not: the
    // sentence names the windows it spent, and the tooltip names them again.
    expect(screen.getByTestId("voice-start").getAttribute("title")).toContain(
      "rather than keep spending calls on a quiet room"
    );
  });

  it("press and hold records, and the release does not also toggle the mute", () => {
    vi.useFakeTimers();
    const wake = wakeWord();
    draw({ wake });
    const button = screen.getByTestId("voice-start");
    fireEvent.pointerDown(button);
    act(() => {
      vi.advanceTimersByTime(HOLD_MS + 10);
    });
    expect(voice.start).toHaveBeenCalledTimes(1);
    voice.status = "recording";
    fireEvent.pointerUp(button);
    expect(voice.stop).toHaveBeenCalledTimes(1);
    fireEvent.click(button);
    expect(wake.setEnabled).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("holding pauses the wake listener, because one microphone is one conversation", () => {
    vi.useFakeTimers();
    const wake = wakeWord({ enabled: true, listening: true, backend: "windows" });
    draw({ wake });
    const button = screen.getByTestId("voice-start");
    fireEvent.pointerDown(button);
    act(() => {
      vi.advanceTimersByTime(HOLD_MS + 10);
    });
    expect(wake.stop).toHaveBeenCalledTimes(1);
    fireEvent.pointerUp(button);
    expect(wake.start).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("shows what it heard on the same control", () => {
    draw({ wake: wakeWord({ justHeard: true, lastHeard: "route the copper" }) });
    expect(screen.getByTestId("voice-heard").textContent).toContain(
      "heard “Ada, route the copper”"
    );
  });

  it("while recording it shows a real clock and the two ways out", () => {
    voice.status = "recording";
    voice.elapsedS = 4;
    draw();
    expect(screen.getByTestId("voice-elapsed").textContent).toContain("4s/");
    expect(screen.getByTestId("voice-stop")).toBeTruthy();
    expect(screen.getByTestId("voice-cancel")).toBeTruthy();
  });

  it("the menu states the cost of unmuting and that holding always works", () => {
    draw();
    fireEvent.contextMenu(screen.getByTestId("voice-control"));
    const menu = screen.getByTestId("voice-menu");
    expect(menu.textContent).toContain("I start muted every time");
    expect(menu.textContent).toContain("one model call");
    expect(menu.textContent).toContain("capped at 15");
    expect(menu.textContent).toContain("Press and hold");
    expect(screen.getByTestId("voice-menu-state").textContent).toBe("I am muted.");
  });

  it("without a wake word it is still a microphone, and a tap records", () => {
    draw({ wake: undefined });
    fireEvent.click(screen.getByTestId("voice-start"));
    expect(voice.start).toHaveBeenCalledTimes(1);
  });

  it("speaks in the first person, and never calls itself an assistant", () => {
    const { container } = draw();
    expect(screen.getByTestId("voice-start").getAttribute("title")).toMatch(/^I am /);
    expect(container.innerHTML.toLowerCase()).not.toMatch(/assistant|helper/);
  });
});

describe("the mute icon", () => {
  it("is struck through whenever nobody is listening, whatever the reason", () => {
    expect(micIsOpen("muted")).toBe(false)
    expect(micIsOpen("spent")).toBe(false)
    expect(micIsOpen("refused")).toBe(false)
    expect(micIsOpen("arming")).toBe(false)
  })

  it("is a plain microphone only while the mic is actually open", () => {
    expect(micIsOpen("unmuted")).toBe(true)
    expect(micIsOpen("heard")).toBe(true)
    expect(micIsOpen("recording")).toBe(true)
  })
})

// The founder's report, and the reason the strip's Stop button could go away:
// once I am talking there must be a way to shut me up, and the strip has one
// control. So the microphone does the obvious thing in each state. Muting the
// ear here would be a click nobody can hear — the duck has already closed the
// microphone for the length of every reply, which is exactly what made the old
// Stop button useless while I spoke.
describe("the one control while I am talking", () => {
  it("stops my voice on a click, and leaves the mute alone", () => {
    const silence = vi.spyOn(speaker, "stop").mockImplementation(() => {});
    const setEnabled = vi.fn();
    try {
      draw({ speaking: true, wake: wakeWord({ enabled: true, setEnabled }) });
      const mic = screen.getByTestId("voice-start");
      expect(mic.getAttribute("data-stops")).toBe("voice");
      fireEvent.click(mic);
      expect(silence).toHaveBeenCalledOnce();
      // The ear is the other organ and this click is not about it.
      expect(setEnabled).not.toHaveBeenCalled();
    } finally {
      silence.mockRestore();
    }
  });

  it("says what the click does, in words, without claiming to touch the ear", () => {
    draw({ speaking: true, wake: wakeWord({ enabled: true }) });
    const title = screen.getByTestId("voice-start").getAttribute("title") ?? "";
    expect(title).toContain("stop me");
    expect(title).toContain("mute is unchanged");
  });

  it("still mutes the ear once I have stopped talking", () => {
    const setEnabled = vi.fn();
    draw({ speaking: false, wake: wakeWord({ enabled: true, listening: true, setEnabled }) });
    const mic = screen.getByTestId("voice-start");
    expect(mic.getAttribute("data-stops")).toBe("mic");
    fireEvent.click(mic);
    expect(setEnabled).toHaveBeenCalledExactlyOnceWith(false);
  });
});
