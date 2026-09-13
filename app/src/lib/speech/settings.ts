// Voice settings, stored the way the engine token is stored: localStorage
// through `safeLocalStorage`, keys namespaced in `KALEO_STORAGE_KEYS`.
//
// The ElevenLabs key lives here under the same reasoning as the engine
// token — this app already keeps provider API keys in localStorage and no JS
// in this build touches the keychain plugin. The one rule that is absolute:
// the key's VALUE never reaches a log line, an error message, or an export.
// Nothing in `src/lib/speech/` interpolates it into anything but the
// `xi-api-key` request header.

import { safeLocalStorage } from "@/lib/storage/helper";
import {
  ELEVENLABS_ENV_VAR,
  KALEO_STORAGE_KEYS,
  KALEO_VOICE_ENV_FILE,
} from "@/config/kaleo.constants";

export interface VoiceSettings {
  /** The on/off switch for the spoken digest. */
  enabled: boolean;
  /** ElevenLabs API key; empty string means "use the browser's own voice". */
  elevenLabsKey: string;
  /** Optional ElevenLabs voice id; empty string means the default voice. */
  voiceId: string;
  /**
   * Has the platform's own `speechSynthesis` been chosen by name? Absent
   * means no — see `isPlatformVoiceAllowed`. Nothing falls through to it.
   */
  platformVoice: boolean;
}

/**
 * Voice defaults ON: the webspeech backend is free, offline and needs no
 * account, so talking and being talked back to is what a fresh install does
 * with nothing configured.
 */
export const VOICE_DEFAULT_ENABLED = true;

/**
 * The stored spellings that mean "I turned this off". More than one because
 * the flag has been written as `"0"` here and could arrive as a `"false"`
 * from an older build or a hand-edited localStorage; a value that was meant
 * as off must never be read as on.
 */
const OFF_VALUES = new Set(["0", "false", "off", "no"]);

/** Has anyone ever made a choice here? Absence is what the default fills. */
export function hasVoicePreference(): boolean {
  return safeLocalStorage.getItem(KALEO_STORAGE_KEYS.VOICE_ENABLED) !== null;
}

/**
 * Is the voice allowed to talk?
 *
 * A stored preference always wins, in both directions. Only its *absence*
 * becomes `VOICE_DEFAULT_ENABLED` — because someone who deliberately silenced
 * this app and then found it talking again on the next release would be right
 * to call that a bug, and changing a default must never reach through an
 * explicit choice. Pinned by test in settings.test.ts.
 */
export function isVoiceEnabled(): boolean {
  const stored = safeLocalStorage.getItem(KALEO_STORAGE_KEYS.VOICE_ENABLED);
  if (stored === null) return VOICE_DEFAULT_ENABLED;
  return !OFF_VALUES.has(stored.trim().toLowerCase());
}

export function saveVoiceEnabled(enabled: boolean): void {
  // "1"/"0" rather than remove-on-true so an explicit choice is
  // distinguishable from "never touched" if the default ever changes.
  safeLocalStorage.setItem(KALEO_STORAGE_KEYS.VOICE_ENABLED, enabled ? "1" : "0");
}

/**
 * May Ada speak with the webview's own `speechSynthesis`?
 *
 * **Absence means no**, which is deliberately the reverse of
 * `isVoiceEnabled` above, and the reversal is the point of this whole
 * module's 2026-09-08 change.
 *
 * On macOS `speechSynthesis` resolves to a *Compact* system voice. Until
 * today it was not merely the fallback but the everyday voice, because
 * `speaker.defaultMakeBackend` read an engine URL that a fresh install never
 * writes, found `""`, and went straight here — so the honest
 * `ServiceVoiceUnavailable` warning that was supposed to announce the
 * downgrade never ran, because no downgrade ever happened. There was nothing
 * to warn about: the robot was the primary path.
 *
 * The fix is not a louder warning. A `console.warn` is invisible in a release
 * webview, and a spoken line is a poor place to carry a disclaimer. The fix
 * is that this voice is never reached by falling: it is reached by choosing.
 * Then it is not a degradation at all, needs no warning, and the only
 * automatic outcome of a missing engine voice is **silence** — which no user
 * has ever mistaken for a working feature.
 */
export function isPlatformVoiceAllowed(): boolean {
  const stored = safeLocalStorage.getItem(KALEO_STORAGE_KEYS.PLATFORM_VOICE_OPT_IN);
  if (stored === null) return false;
  return !OFF_VALUES.has(stored.trim().toLowerCase());
}

export function savePlatformVoiceAllowed(allowed: boolean): void {
  safeLocalStorage.setItem(
    KALEO_STORAGE_KEYS.PLATFORM_VOICE_OPT_IN,
    allowed ? "1" : "0"
  );
}

// ---------------------------------------------------------------------------
// The key from the environment
// ---------------------------------------------------------------------------
//
// A Tauri webview cannot read process env: there is no `process`, and
// `std::env` is on the Rust side of an IPC boundary. So "read
// ELEVENLABS_API_KEY from the environment" means, in this repository,
// `~/.kaleo/voice.env` — the same `KEY=value` file family
// `service/envfiles.py` already defines for billing, google and microsoft,
// read through the fs plugin. Two properties are inherited deliberately:
// setdefault precedence (an explicit setting outranks a file left on a
// laptop, which is why localStorage wins below), and the file being read at
// an explicit moment rather than at import.
//
// It is cached because it is a file read on the way to every utterance, and
// `null` versus `""` is a real distinction here: `null` means "not looked
// yet", `""` means "looked, and there is no key" — without that a machine
// with no file would re-open it before every digest forever.

let cachedEnvKey: string | null = null;
let hydration: Promise<string> | null = null;

/** Never let a stuck filesystem call become a voice that will not start. */
export const VOICE_ENV_READ_TIMEOUT_MS = 2_000;

/**
 * Parse the `KEY=value` shape `service/envfiles.py` writes.
 *
 * Deliberately the same tolerances as `wake/config.rs::apply_dotenv`, which
 * is the other reader of this shape in the repo: blank lines and `#` comments
 * skipped, `export ` prefix tolerated, surrounding quotes stripped, first `=`
 * wins so a base64 value containing `=` survives. Anything it cannot parse is
 * skipped rather than thrown — a malformed line must not silence the app.
 */
export function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const body = line.startsWith("export ") ? line.slice(7).trim() : line;
    const eq = body.indexOf("=");
    if (eq <= 0) continue;
    const key = body.slice(0, eq).trim();
    if (!key) continue;
    let value = body.slice(eq + 1).trim();
    if (value.length >= 2) {
      const first = value[0];
      if ((first === '"' || first === "'") && value.endsWith(first)) {
        value = value.slice(1, -1);
      }
    }
    out[key] = value;
  }
  return out;
}

/** The seam: production reads `~/.kaleo/voice.env`, tests hand over a string. */
export type VoiceEnvReader = () => Promise<string | null>;

async function readVoiceEnvFile(): Promise<string | null> {
  // Dynamic import so a browser/vitest build with no Tauri runtime resolves
  // to "no env key" instead of failing to load this module at all.
  const { readFile, BaseDirectory } = await import("@tauri-apps/plugin-fs");
  const bytes = await readFile(KALEO_VOICE_ENV_FILE, {
    baseDir: BaseDirectory.Home,
  });
  return new TextDecoder().decode(bytes);
}

/**
 * Look for the key on disk, once. Resolves to `""` when there is none.
 *
 * Never rejects and never hangs: a missing file, a webview without the fs
 * plugin, a permission refusal and a wedged filesystem are all the same
 * answer — no key, use the free voice. The result is cached either way, so
 * calling this before every utterance is free after the first.
 *
 * NOTHING here logs the value, and the caught error is discarded rather than
 * inspected: an fs error message can quote the file's contents.
 */
export function hydrateVoiceEnv(reader: VoiceEnvReader = readVoiceEnvFile): Promise<string> {
  if (cachedEnvKey !== null) return Promise.resolve(cachedEnvKey);
  if (hydration) return hydration;
  hydration = (async () => {
    let text: string | null = null;
    try {
      text = await Promise.race([
        reader(),
        new Promise<null>((resolve) =>
          setTimeout(() => resolve(null), VOICE_ENV_READ_TIMEOUT_MS)
        ),
      ]);
    } catch {
      text = null;
    }
    const key = text ? (parseEnvFile(text)[ELEVENLABS_ENV_VAR] ?? "").trim() : "";
    cachedEnvKey = key;
    hydration = null;
    return key;
  })();
  return hydration;
}

/** Test seam only: forget what was read. */
export function resetVoiceEnvForTests(): void {
  cachedEnvKey = null;
  hydration = null;
}

/** What hydration found, without waiting. `""` until it has finished. */
export function voiceEnvKey(): string {
  return cachedEnvKey ?? "";
}

/**
 * The key, with an explicit setting outranking the environment.
 *
 * That order is `envfiles.apply_env`'s setdefault rule seen from the other
 * side: there, a process value wins over a file; here, the value a human
 * typed into this app wins over a file they may have forgotten is on the
 * machine. Both say the more deliberate act wins.
 */
export function loadElevenLabsKey(): string {
  const stored = (
    safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY) ?? ""
  ).trim();
  return stored || voiceEnvKey();
}

export function saveElevenLabsKey(key: string): void {
  const trimmed = key.trim();
  if (trimmed) {
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY, trimmed);
  } else {
    // An empty field means "back to the free voice", not "store an empty key".
    safeLocalStorage.removeItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY);
  }
}

export function loadElevenLabsVoiceId(): string {
  return (
    safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID) ?? ""
  ).trim();
}

export function saveElevenLabsVoiceId(voiceId: string): void {
  const trimmed = voiceId.trim();
  if (trimmed) {
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID, trimmed);
  } else {
    safeLocalStorage.removeItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID);
  }
}

export function loadVoiceSettings(): VoiceSettings {
  return {
    enabled: isVoiceEnabled(),
    elevenLabsKey: loadElevenLabsKey(),
    voiceId: loadElevenLabsVoiceId(),
    platformVoice: isPlatformVoiceAllowed(),
  };
}
