// @vitest-environment jsdom
//
// The one test that matters here is the third one: flipping a default so
// that it reaches through somebody's explicit choice is not a new default,
// it is a bug with a changelog entry. Someone who silenced this app in a
// shared office and found it talking again after an update would be right
// to call it that, so the distinction between "chose off" and "never chose"
// is pinned rather than left to the reading of a `!== "0"`.

import { beforeEach, describe, expect, it } from "vitest";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import {
  VOICE_DEFAULT_ENABLED,
  hasVoicePreference,
  isVoiceEnabled,
  saveVoiceEnabled,
} from "./settings";

const KEY = KALEO_STORAGE_KEYS.VOICE_ENABLED;

beforeEach(() => {
  window.localStorage.clear();
});

describe("isVoiceEnabled", () => {
  it("speaks by default on a fresh install, with nothing stored", () => {
    expect(hasVoicePreference()).toBe(false);
    expect(VOICE_DEFAULT_ENABLED).toBe(true);
    expect(isVoiceEnabled()).toBe(true);
  });

  it("keeps someone who turned it off, off — the default never overrides a choice", () => {
    saveVoiceEnabled(false);
    expect(hasVoicePreference()).toBe(true);
    expect(isVoiceEnabled()).toBe(false);
    // And it stays off across as many reads as a session makes.
    expect(isVoiceEnabled()).toBe(false);
  });

  it("distinguishes a stored off from an absent preference", () => {
    // Same answer would be given by a default of ON in both cases — which is
    // exactly the confusion this pair of functions exists to prevent.
    expect(hasVoicePreference()).toBe(false);
    window.localStorage.setItem(KEY, "0");
    expect(hasVoicePreference()).toBe(true);
    expect(isVoiceEnabled()).toBe(false);
    window.localStorage.removeItem(KEY);
    expect(hasVoicePreference()).toBe(false);
    expect(isVoiceEnabled()).toBe(true);
  });

  it("reads an explicit on as on", () => {
    saveVoiceEnabled(true);
    expect(window.localStorage.getItem(KEY)).toBe("1");
    expect(isVoiceEnabled()).toBe(true);
  });

  it("honours an off written in an older spelling rather than reading it as on", () => {
    for (const off of ["false", "off", "no", " 0 ", "FALSE"]) {
      window.localStorage.setItem(KEY, off);
      expect(isVoiceEnabled()).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// The key from `~/.kaleo/voice.env`
// ---------------------------------------------------------------------------
//
// Nothing here opens a real file and nothing asserts a key's value into a
// message: the reader is a seam, and the assertions are about which source
// won and whether the app stayed quiet when there was no source at all.

import { vi } from "vitest";
import { KALEO_STORAGE_KEYS as KEYS } from "@/config/kaleo.constants";
import {
  VOICE_ENV_READ_TIMEOUT_MS,
  hydrateVoiceEnv,
  loadElevenLabsKey,
  parseEnvFile,
  resetVoiceEnvForTests,
  voiceEnvKey,
} from "./settings";

beforeEach(() => {
  resetVoiceEnvForTests();
});

describe("parseEnvFile", () => {
  it("reads the shape service/envfiles.py writes", () => {
    expect(
      parseEnvFile(
        [
          "# Written by Hardy's Setup Assistant. Never commit this file.",
          "",
          "ELEVENLABS_API_KEY=abc123",
          'export OTHER="quoted value"',
          "PADDED =  spaced  ",
        ].join("\n")
      )
    ).toEqual({
      ELEVENLABS_API_KEY: "abc123",
      OTHER: "quoted value",
      PADDED: "spaced",
    });
  });

  it("keeps a value containing '=', and skips what it cannot parse", () => {
    // Base64 and JWT-ish credentials are full of '='; splitting on the last
    // one, or on all of them, would hand over a truncated key that fails at
    // the API with a 401 and no clue why.
    const parsed = parseEnvFile(
      ["ELEVENLABS_API_KEY=a=b==", "nonsense line", "=novalue", "#c=d"].join("\n")
    );
    expect(parsed.ELEVENLABS_API_KEY).toBe("a=b==");
    expect(Object.keys(parsed)).toEqual(["ELEVENLABS_API_KEY"]);
  });
});

describe("hydrateVoiceEnv", () => {
  it("finds the key in the file, and reads the file only once", async () => {
    const reader = vi.fn(async () => "ELEVENLABS_API_KEY=from-the-env-file\n");
    expect(await hydrateVoiceEnv(reader)).toBe("from-the-env-file");
    expect(await hydrateVoiceEnv(reader)).toBe("from-the-env-file");
    // Cached: this is on the path to every utterance.
    expect(reader).toHaveBeenCalledTimes(1);
    expect(voiceEnvKey()).toBe("from-the-env-file");
  });

  it("treats every way of not finding one as the same answer: no key", async () => {
    // No file, no fs plugin, a permission refusal — all indistinguishable to
    // a user, all meaning "use the free voice", none of them an exception.
    for (const reader of [
      async () => null,
      async () => "",
      async () => "SOMETHING_ELSE=1\n",
      async () => {
        throw new Error("ENOENT: no such file or directory");
      },
    ]) {
      resetVoiceEnvForTests();
      await expect(hydrateVoiceEnv(reader)).resolves.toBe("");
    }
  });

  it("gives up rather than hanging on a wedged filesystem", async () => {
    vi.useFakeTimers();
    try {
      const hydrating = hydrateVoiceEnv(() => new Promise<string>(() => {}));
      await vi.advanceTimersByTimeAsync(VOICE_ENV_READ_TIMEOUT_MS + 1);
      await expect(hydrating).resolves.toBe("");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("loadElevenLabsKey", () => {
  it("prefers what a human typed here over what a file on the machine says", async () => {
    // `envfiles.apply_env`'s setdefault rule, seen from the other side: the
    // more deliberate act wins. Someone who pasted a key into this app should
    // not be silently overridden by a voice.env they set up months ago.
    await hydrateVoiceEnv(async () => "ELEVENLABS_API_KEY=from-file\n");
    expect(loadElevenLabsKey()).toBe("from-file");
    window.localStorage.setItem(KEYS.ELEVENLABS_KEY, "from-settings");
    expect(loadElevenLabsKey()).toBe("from-settings");
  });

  it("is empty when neither source has one", () => {
    expect(loadElevenLabsKey()).toBe("");
  });
});
