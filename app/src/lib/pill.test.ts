import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

import { earWord, runWord } from "./pill";

const off = {
  enabled: false,
  listening: false,
  justHeard: false,
  lastHeard: null,
  error: null,
  backend: null,
  sent: 0,
  cap: 15,
};

describe("earWord", () => {
  it("says the ear is off, and why when it stopped itself", () => {
    expect(earWord(off)).toBe("Ada off");
    expect(earWord(null)).toBe("Ada off");
    expect(earWord({ ...off, error: "the engine refused the audio" })).toBe(
      "Ada off — the engine refused the audio"
    );
  });

  it("spells out the bill while the paid backend is listening", () => {
    expect(
      earWord({ ...off, enabled: true, listening: true, backend: "windows", sent: 3 })
    ).toBe("Listening for “Ada” · 3/15 paid windows");
  });

  it("separates the free recognizers from the paid one", () => {
    expect(
      earWord({ ...off, enabled: true, listening: true, backend: "speech" })
    ).toBe("Listening for “Ada” · OS");
    expect(
      earWord({ ...off, enabled: true, listening: true, backend: "local" })
    ).toBe("Listening for “Ada” · on-device");
  });

  it("says the mic is opening rather than claiming it is already hearing", () => {
    expect(earWord({ ...off, enabled: true })).toBe("Opening the mic for one listen");
  });

  it("echoes what it heard", () => {
    expect(
      earWord({ ...off, justHeard: true, lastHeard: "make me a 3.3 V LDO" })
    ).toBe("heard “Ada, make me a 3.3 V LDO”");
    expect(earWord({ ...off, justHeard: true })).toBe(
      "heard “Ada” — tell me what you need"
    );
  });
});

describe("runWord", () => {
  const up = { status: "idle", stepsStatus: "idle", engineOk: true, engineSettled: true };

  it("does not guess before the first probe lands", () => {
    expect(runWord({ ...up, engineSettled: false, engineOk: false })).toBe(
      "Checking the engine"
    );
  });

  it("puts a dead engine above everything else, because nothing else can be true", () => {
    expect(runWord({ ...up, engineOk: false, stepsStatus: "waiting" })).toBe(
      "Engine down"
    );
  });

  it("names each run state", () => {
    expect(runWord({ ...up, stepsStatus: "running" })).toBe("Working");
    expect(runWord({ ...up, status: "running" })).toBe("Working");
    expect(runWord({ ...up, stepsStatus: "waiting" })).toBe("Waiting for you");
    expect(runWord({ ...up, stepsStatus: "error" })).toBe("A stage failed");
    expect(runWord({ ...up, status: "done" })).toBe("Run finished");
  });

  it("prints an idle state rather than leaving the pill blank", () => {
    expect(runWord(up)).toBe("Nothing running");
  });
});
