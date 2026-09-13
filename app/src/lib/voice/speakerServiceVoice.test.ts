// The service-voice branch of `speech/speaker.ts`.
//
// Kept in this directory rather than appended to `speech/speaker.test.ts` so
// the service-voice work stays in one place and out of a file another agent
// has been editing. What it pins is the honesty rule, not the plumbing: when
// the engine has no voice, Ada must STAY SILENT and say why — she must not
// quietly hand the line to the Compact system voice. That is the exact
// failure this change exists to remove: for weeks the user heard the robot
// and nothing anywhere explained that it was a fallback at all.
//
// It also pins the bug underneath that, which was not a policy question but
// a typo-grade defect: `speaker.engineBaseUrl` read localStorage raw, so on
// any install where nobody had hand-typed an engine address it returned
// `""`, the service backend was never even constructed, and `POST /speak`
// was never called once. See the last test in this file.

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { DEFAULT_BASE_URL } from "@/lib/silkscreen/client";
import { createSpeaker, resetServiceVoice } from "@/lib/speech/speaker";
import type { SpeechBackend } from "@/lib/speech/backends";
import type { VoiceSettings } from "@/lib/speech/settings";
import { ServiceVoiceUnavailable } from "./serviceBackend";

const settings = (overrides: Partial<VoiceSettings> = {}): VoiceSettings => ({
  enabled: true,
  elevenLabsKey: "",
  voiceId: "",
  // Absence means "nobody chose the platform voice", which is the default a
  // fresh install has and therefore the one these tests should exercise.
  platformVoice: false,
  ...overrides,
});

/** A backend that always reports "the engine has no voice configured". */
function unavailableBackend(): SpeechBackend {
  return {
    name: "service",
    speak: async () => {
      throw new ServiceVoiceUnavailable("no speech engine is configured");
    },
    stop: () => {},
  };
}

beforeEach(() => {
  resetServiceVoice();
});

describe("service voice fall-through", () => {
  // This test used to assert the opposite: that an engine with no voice was
  // finished off by `speechSynthesis`. That contract is what made the macOS
  // Compact voice Ada's everyday voice, so it is now reversed — a missing
  // engine voice produces SILENCE unless someone chose the platform voice by
  // name. See `settings.isPlatformVoiceAllowed` for why a louder warning was
  // not judged sufficient.
  it("stays SILENT and says so when the engine has no voice and nobody chose one", async () => {
    const warn = vi.fn();
    const fallback = { name: "webspeech" as const, speak: vi.fn(async () => {}), stop: vi.fn() };
    const speaker = createSpeaker({
      getSettings: () => settings({ platformVoice: false }),
      makeBackend: unavailableBackend,
      // The real `defaultMakeFallback` returns null here; this pins that the
      // speaker honours a null and does not reach for a voice it was handed.
      makeFallback: () => null,
      warn,
    });

    await speaker.speak("Placement is done.");

    expect(fallback.speak).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
    const message = warn.mock.calls[0][0] as string;
    expect(message).toMatch(/no voice provisioned/i);
    expect(message).toMatch(/staying silent/i);
    // It names WHY, carried up from the service's own 503 body.
    expect(message).toContain("no speech engine is configured");
  });

  it("uses the platform voice, without apology, when it was chosen by name", async () => {
    // Opted in, so this is not a degradation and must not be worded as one.
    const warn = vi.fn();
    const fallback = { name: "webspeech" as const, speak: vi.fn(async () => {}), stop: vi.fn() };
    const speaker = createSpeaker({
      getSettings: () => settings({ platformVoice: true }),
      makeBackend: unavailableBackend,
      makeFallback: () => fallback,
      warn,
    });

    await speaker.speak("Placement is done.");

    expect(fallback.speak).toHaveBeenCalledWith("Placement is done.");
    const message = warn.mock.calls[0][0] as string;
    expect(message).toMatch(/platform voice you selected/i);
    expect(message).not.toMatch(/staying silent/i);
  });

  it("reads as a downgrade, not as a crash", async () => {
    // "text-to-speech failed" is the wrong words for a service that is
    // working correctly and simply has no engine provisioned.
    const warn = vi.fn();
    const speaker = createSpeaker({
      getSettings: () => settings(),
      makeBackend: unavailableBackend,
      makeFallback: () => ({ name: "webspeech", speak: async () => {}, stop: () => {} }),
      warn,
    });
    await speaker.speak("V bus.");
    expect(warn.mock.calls[0][0]).not.toMatch(/failed/i);
  });

  it("never throws to the caller", async () => {
    // Voice is garnish, not the meal — the file's own rule.
    const speaker = createSpeaker({
      getSettings: () => settings(),
      makeBackend: unavailableBackend,
      makeFallback: () => ({
        name: "webspeech",
        speak: async () => {
          throw new Error("no speechSynthesis either");
        },
        stop: () => {},
      }),
      warn: () => {},
    });
    await expect(speaker.speak("V bus.")).resolves.toBeUndefined();
  });

  it("a genuine failure still reads as a failure", async () => {
    const warn = vi.fn();
    const speaker = createSpeaker({
      getSettings: () => settings(),
      makeBackend: () => ({
        name: "service",
        speak: async () => {
          throw new Error("the speech host refused the request: HTTP 401");
        },
        stop: () => {},
      }),
      makeFallback: () => ({ name: "webspeech", speak: async () => {}, stop: () => {} }),
      warn,
    });
    await speaker.speak("V bus.");
    const message = warn.mock.calls[0][0] as string;
    expect(message).toMatch(/failed/i);
    expect(message).toContain("401");
  });

  // ---------------------------------------------------------------- the bug
  //
  // The regression test for the defect that made every other honesty rule in
  // this file moot. Verified against the running app's own localStorage on
  // 2026-09-08: the keys present were `silkscreen_overlay_skin`,
  // `silkscreen_tour` and `silkscreen_last_run`. `silkscreen_engine_base_url`
  // was absent, because nothing writes it until someone opens the Engine pane
  // and types an address — so `engineBaseUrl()` returned `""`, `defaultMakeBackend`
  // skipped the service branch entirely, and Ada spoke every word she has ever
  // spoken on that machine through `speechSynthesis`, while Kokoro sat loaded
  // and answering `GET /speak` with `"state": "ready"`.
  //
  // Asserting on the fetch URL rather than on the backend's `name` is
  // deliberate: `name` would have been `"service"` for a backend pointed at
  // `""`, which is exactly the kind of green test that let this ship.
  it("reaches the engine with NO engine URL in storage, rather than the platform voice", async () => {
    // No `localStorage.clear()` and no jsdom: this file runs in the `node`
    // environment, where there is no `localStorage` at all and
    // `safeLocalStorage` therefore answers null for every key. That is the
    // strongest possible form of "nobody ever saved an engine address".
    const speaker = createSpeaker({
      getSettings: () => settings(),
      // `makeBackend` deliberately NOT injected: the default is under test.
      warn: () => {},
      hydrateEnv: async () => "",
    });

    await speaker.speak("Placement is done.");

    expect(tauriFetch).toHaveBeenCalled();
    const url = String(vi.mocked(tauriFetch).mock.calls[0][0]);
    expect(url).toBe(`${DEFAULT_BASE_URL}/speak`);
  });
});
