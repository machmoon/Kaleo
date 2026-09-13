// The settings schema: every key the store may hold, its default, and the
// validator that keeps a hand-edited `settings.json` or a stale mirror from
// poisoning the cache. Theme, overlay skin and voice are deliberately NOT
// here — they keep their existing localStorage paths (`theme`,
// `silkscreen_overlay_skin`, `silkscreen_voice_enabled`), because the code
// that writes them writes only localStorage and a mirror nobody reads is a
// second source of truth waiting to disagree.

import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

/** The file the Rust side builds with autosave; the only path ever passed to `Store.load`. */
export const SETTINGS_STORE_FILE = "settings.json";

/** Bumped when the Setup Assistant gains a step worth offering to existing users. */
export const SETUP_VERSION = 1;

export type NotifyOs = "always" | "not_focused" | "never";

export interface SettingsSchema {
  /** Native banners on or off. Default off: the test button is the opt-in. */
  "notify.enabled": boolean;
  /** When a banner may fire relative to Kaleo having focus. */
  "notify.os": NotifyOs;
  /** The Rust gate reads this exact key; anything but `true` gates the strip. */
  "setup.completed": boolean;
  /** Which assistant version last completed; below `SETUP_VERSION` is a "Run Setup Again" hint. */
  "setup.version": number;
  /** Resume point, a step id owned by `src/lib/setup/machine.ts`. */
  "setup.step": string;
  /** Card ids not yet decided, persisted after every reduce so a close mid-setup can skip them. */
  "setup.remaining": string[];
  /** Card ids (`google|microsoft|stripe|notifications|voice`) the user chose to set up later. */
  "setup.skipped": string[];
  /** Epoch ms when setup finished, 0 when it never has. */
  "setup.completedAt": number;
  "tour.completed": boolean;
}

export type SettingKey = keyof SettingsSchema;

export const SETTING_DEFAULTS: Readonly<SettingsSchema> = Object.freeze({
  "notify.enabled": false,
  "notify.os": "not_focused",
  "setup.completed": false,
  "setup.version": 0,
  "setup.step": "hello",
  "setup.remaining": [],
  "setup.skipped": [],
  "setup.completedAt": 0,
  "tour.completed": false,
});

export const SETTING_KEYS: readonly SettingKey[] = Object.freeze(
  Object.keys(SETTING_DEFAULTS) as SettingKey[]
);

export function isSettingKey(key: string): key is SettingKey {
  return Object.prototype.hasOwnProperty.call(SETTING_DEFAULTS, key);
}

/** The localStorage key one setting is mirrored under. */
export function mirrorKey(key: SettingKey): string {
  return `${KALEO_STORAGE_KEYS.SETTINGS_MIRROR_PREFIX}${key}`;
}

/** The setting a mirror key belongs to, or null for anything else in localStorage. */
export function settingOfMirrorKey(storageKey: string | null): SettingKey | null {
  const prefix = KALEO_STORAGE_KEYS.SETTINGS_MIRROR_PREFIX;
  if (!storageKey || !storageKey.startsWith(prefix)) return null;
  const key = storageKey.slice(prefix.length);
  return isSettingKey(key) ? key : null;
}

const NOTIFY_OS_VALUES: readonly NotifyOs[] = ["always", "not_focused", "never"];

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === "string");
}

/**
 * Is `value` an acceptable value for `key`? Type-checked against the
 * default's shape, plus the enum for `notify.os`. Anything else — a string
 * where a boolean belongs, a NaN, an array of numbers — is refused and the
 * reader falls back to the default rather than storing it.
 */
export function isValidSetting<K extends SettingKey>(
  key: K,
  value: unknown
): value is SettingsSchema[K] {
  switch (key) {
    case "notify.os":
      return typeof value === "string" && (NOTIFY_OS_VALUES as readonly string[]).includes(value);
    case "setup.remaining":
    case "setup.skipped":
      return isStringArray(value);
    case "setup.version":
    case "setup.completedAt":
      return typeof value === "number" && Number.isFinite(value);
    case "setup.step":
      return typeof value === "string";
    default:
      return typeof value === typeof SETTING_DEFAULTS[key];
  }
}

/** Parse a mirror string; `undefined` when it is absent, unparsable or invalid for `key`. */
export function parseMirror<K extends SettingKey>(
  key: K,
  raw: string | null
): SettingsSchema[K] | undefined {
  if (raw === null) return undefined;
  try {
    const value: unknown = JSON.parse(raw);
    return isValidSetting(key, value) ? value : undefined;
  } catch {
    return undefined;
  }
}
