// The gate in front of the mouth: mute, right of way, ducking, availability.
//
// Nothing here makes a sound. The speaker is a fake with a resolvable
// promise, which is also the only way to test "yields while the human is
// talking" — the interesting states are all about a sentence that is NOT
// said, and silence is not observable by listening.

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import {
  announce,
  isAnnouncing,
  micHasFloor,
  resetMicFloor,
  setSpeechDuck,
  subscribeSpeaking,
  takeMicFloor,
} from "./announce";
import type { Speaker } from "./speaker";

function fakeSpeaker(): Speaker & { said: string[]; stops: number; release: () => void } {
  let finish: (() => void) | null = null;
  const said: string[] = [];
  return {
    said,
    stops: 0,
    speak(text: string) {
      said.push(text);
      return new Promise<void>((resolve) => {
        finish = resolve;
      });
    },
    stop() {
      (this as { stops: number }).stops += 1;
      finish?.();
      finish = null;
    },
    isSpeaking: () => finish !== null,
    // Added by Agent F with the speaker queue: this fake speaks immediately,
    // so nothing is ever waiting behind it. Mechanical — no behaviour here
    // changes.
    pending: () => 0,
    release() {
      finish?.();
      finish = null;
    },
  };
}

const on = () => true;
const off = () => false;
const available = () => true;

beforeEach(() => {
  resetMicFloor();
  setSpeechDuck(null);
});

describe("announce", () => {
  it("says the line when the voice is on and nothing else holds the floor", async () => {
    const speaker = fakeSpeaker();
    const spoken = announce("Placement is done.", { speaker, enabled: on, available });
    speaker.release();
    expect(await spoken).toBe("spoken");
    expect(speaker.said).toEqual(["Placement is done."]);
  });

  it("says nothing at all when the voice is silenced", async () => {
    const speaker = fakeSpeaker();
    expect(await announce("Placement is done.", { speaker, enabled: off, available })).toBe(
      "muted"
    );
    expect(speaker.said).toEqual([]);
  });

  it("yields to the microphone rather than talking over the human", async () => {
    const speaker = fakeSpeaker();
    const release = takeMicFloor(speaker);
    expect(micHasFloor()).toBe(true);
    // Dropped, not queued: by the time the floor is free this sentence is
    // about something that already happened.
    expect(await announce("Placement is done.", { speaker, enabled: on, available })).toBe(
      "yielded"
    );
    expect(speaker.said).toEqual([]);
    release();
    expect(micHasFloor()).toBe(false);
  });

  it("cuts an utterance in progress the moment the microphone takes the floor", async () => {
    const speaker = fakeSpeaker();
    const spoken = announce("A long sentence.", { speaker, enabled: on, available });
    expect(speaker.said).toEqual(["A long sentence."]);
    takeMicFloor(speaker);
    expect(speaker.stops).toBeGreaterThan(0);
    expect(await spoken).toBe("spoken");
  });

  it("releases the floor idempotently, so a doubled pointer-up cannot mute the app", async () => {
    const speaker = fakeSpeaker();
    const release = takeMicFloor(speaker);
    const other = takeMicFloor(speaker);
    release();
    release();
    release();
    expect(micHasFloor()).toBe(true);
    other();
    expect(micHasFloor()).toBe(false);
  });

  it("ducks the listener while speaking and starts it again after", async () => {
    const speaker = fakeSpeaker();
    const events: string[] = [];
    setSpeechDuck(() => {
      events.push("stop");
      return () => events.push("start");
    });
    const spoken = announce("Routing failed.", { speaker, enabled: on, available });
    expect(events).toEqual(["stop"]);
    speaker.release();
    await spoken;
    expect(events).toEqual(["stop", "start"]);
  });

  it("does not re-arm the listener when a human took the microphone mid-sentence", async () => {
    const speaker = fakeSpeaker();
    const events: string[] = [];
    setSpeechDuck(() => {
      events.push("stop");
      return () => events.push("start");
    });
    const spoken = announce("A long sentence.", { speaker, enabled: on, available });
    // Push-to-talk: it cuts the utterance and owns the microphone now. It
    // restarts the ear itself on release; arming it under a live recording
    // is the failure this branch exists to avoid.
    takeMicFloor(speaker);
    await spoken;
    expect(events).toEqual(["stop"]);
  });

  it("does not duck the listener for a line it was never going to say", async () => {
    const speaker = fakeSpeaker();
    const events: string[] = [];
    setSpeechDuck(() => {
      events.push("stop");
      return () => events.push("start");
    });
    await announce("Placement is done.", { speaker, enabled: off, available });
    expect(events).toEqual([]);
  });

  it("reports unavailable rather than appearing to speak with no voice", async () => {
    const speaker = fakeSpeaker();
    expect(
      await announce("Placement is done.", {
        speaker,
        enabled: on,
        available: () => false,
      })
    ).toBe("unavailable");
    expect(speaker.said).toEqual([]);
  });

  it("treats an empty line as nothing to say", async () => {
    const speaker = fakeSpeaker();
    expect(await announce("   ", { speaker, enabled: on, available })).toBe("empty");
    expect(speaker.said).toEqual([]);
  });

  it("publishes that it is speaking, so the strip need not read the ducked ear", async () => {
    const speaker = fakeSpeaker();
    const seen: boolean[] = [];
    const unsubscribe = subscribeSpeaking((value) => seen.push(value));
    expect(isAnnouncing()).toBe(false);
    const spoken = announce("Placement is done.", { speaker, enabled: on, available });
    expect(isAnnouncing()).toBe(true);
    speaker.release();
    await spoken;
    expect(isAnnouncing()).toBe(false);
    expect(seen).toEqual([true, false]);
    unsubscribe();
  });

  it("never reports speaking for a line it declined to say", async () => {
    const speaker = fakeSpeaker();
    const seen: boolean[] = [];
    const unsubscribe = subscribeSpeaking((value) => seen.push(value));
    await announce("Placement is done.", { speaker, enabled: off, available });
    expect(seen).toEqual([]);
    expect(isAnnouncing()).toBe(false);
    unsubscribe();
  });

  it("survives a duck that throws instead of going mute", async () => {
    const speaker = fakeSpeaker();
    setSpeechDuck(() => {
      throw new Error("the ear is on fire");
    });
    const spoken = announce("Placement is done.", { speaker, enabled: on, available });
    speaker.release();
    expect(await spoken).toBe("spoken");
  });
});
