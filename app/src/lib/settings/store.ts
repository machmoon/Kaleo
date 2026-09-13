// The settings store: a synchronous read cache and a localStorage mirror in
// front of `@tauri-apps/plugin-store` (`settings.json` in `app_data_dir`).
//
// Three readers have to agree on one value: the React tree at first paint
// (synchronous, before any plugin round trip), the other webview (the strip
// and the dashboard are separate windows sharing one bundle), and the Rust
// gate that decides whether the strip is shown at all (`setup.completed`).
// The cache answers the first, the mirror plus the `storage` event answer the
// second, and the plugin — the only writer that reaches disk — answers the
// third. Outside Tauri (Vitest, a plain browser) the backend is memory and
// the plugin module is never even resolved: it is loaded by dynamic import
// behind `isTauriRuntime()`.
//
// Cross-window change propagation is belt and braces on purpose. The plugin's
// `onChange` is a global `listen("store://change")` filtered on the store's
// resource id; whether both webviews see the same resource id depends on the
// Rust side caching one store per path, which the JS cannot verify. So the
// mirror's `storage` event is listened to as well, and every notification is
// deduped with `Object.is` against the cache, so hearing a change twice costs
// nothing and hearing it once from either channel is enough.

import { safeLocalStorage } from "@/lib/storage/helper";
import {
  SETTING_DEFAULTS,
  SETTING_KEYS,
  SETTINGS_STORE_FILE,
  isValidSetting,
  mirrorKey,
  parseMirror,
  settingOfMirrorKey,
  type SettingKey,
  type SettingsSchema,
} from "./keys";

export type {
  NotifyOs,
  SettingKey,
  SettingsSchema,
} from "./keys";
export { SETTING_DEFAULTS, SETTINGS_STORE_FILE, SETUP_VERSION } from "./keys";

/** The Tauri event the Rust side emits when it wrote `setup.*` itself. */
export const SETUP_CHANGED_EVENT = "kaleo-setup-changed";

/** True inside a Tauri webview; false in Vitest and any plain browser. */
export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * What a backend has to provide. `load` answers with every key it holds (an
 * absent key means "never written", which the existing-user check relies on);
 * `onChange`, when present, reports writes made by someone else.
 */
export interface SettingsBackend {
  load(): Promise<Partial<Record<SettingKey, unknown>>>;
  set(key: SettingKey, value: unknown): Promise<void>;
  onChange?(cb: (key: string, value: unknown) => void): Promise<() => void>;
}

export interface SettingsApi {
  getSetting<K extends SettingKey>(key: K): SettingsSchema[K];
  setSetting<K extends SettingKey>(key: K, value: SettingsSchema[K]): Promise<void>;
  subscribe<K extends SettingKey>(key: K, cb: (value: SettingsSchema[K]) => void): () => void;
  initSettings(): Promise<void>;
  /** Re-read the backend into the cache (what the `kaleo-setup-changed` event triggers). */
  hydrate(): Promise<void>;
}

export interface CreateSettingsStoreOptions {
  /**
   * Whether the current process is the app (dispatches `setup_finish`,
   * listens for `kaleo-setup-changed`). Defaults to `isTauriRuntime()`;
   * tests pass `false` explicitly so no `invoke` is ever attempted.
   */
  tauri?: boolean;
  /** Seam for the existing-user check; defaults to scanning localStorage for `silkscreen_*`. */
  hasLegacyKeys?: () => boolean;
  /** Seam for `invoke("setup_finish", …)`; defaults to the real one inside Tauri. */
  finishSetup?: (skipped: string[]) => Promise<void>;
  /** Seam for the Tauri event subscription; defaults to `listen` inside Tauri. */
  listenSetupChanged?: (cb: () => void) => Promise<() => void>;
}

/** A backend that forgets everything when the page does. */
export function memoryBackend(
  initial: Partial<Record<SettingKey, unknown>> = {}
): SettingsBackend {
  const data = new Map<SettingKey, unknown>(
    Object.entries(initial) as [SettingKey, unknown][]
  );
  return {
    async load() {
      return Object.fromEntries(data) as Partial<Record<SettingKey, unknown>>;
    },
    async set(key, value) {
      data.set(key, value);
    },
  };
}

/**
 * The real backend. The plugin is imported here and nowhere else, and
 * `SETTINGS_STORE_FILE` is the only path ever passed to `load`: the Rust
 * side builds that same store with autosave, and the plugin hands back the
 * cached instance rather than a second one that would never reach disk.
 */
async function tauriBackend(): Promise<SettingsBackend> {
  const { Store } = await import("@tauri-apps/plugin-store");
  const store = await Store.load(SETTINGS_STORE_FILE, { autoSave: 100 });
  return {
    async load() {
      const out: Partial<Record<SettingKey, unknown>> = {};
      for (const [key, value] of await store.entries()) {
        if (SETTING_KEYS.includes(key as SettingKey)) out[key as SettingKey] = value;
      }
      return out;
    },
    set(key, value) {
      return store.set(key, value);
    },
    onChange(cb) {
      return store.onChange((key, value) => cb(key, value));
    },
  };
}

/** Any `silkscreen_*` key in localStorage: a build before the store existed ran here. */
export function hasLegacySilkscreenKeys(): boolean {
  if (typeof window === "undefined") return false;
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith("silkscreen_")) return true;
    }
  } catch {
    // Storage disabled: nothing to detect, and a fresh gate is the safe answer.
  }
  return false;
}

async function invokeSetupFinish(skipped: string[]): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("setup_finish", { skipped });
}

async function listenSetupChangedEvent(cb: () => void): Promise<() => void> {
  const { listen } = await import("@tauri-apps/api/event");
  return listen(SETUP_CHANGED_EVENT, () => cb());
}

/**
 * Build a settings API over one backend. The module-level functions below
 * are this over the real backend; tests build their own over `memoryBackend`.
 */
export function createSettingsStore(
  backend: SettingsBackend | Promise<SettingsBackend>,
  options: CreateSettingsStoreOptions = {}
): SettingsApi {
  const tauri = options.tauri ?? isTauriRuntime();
  const hasLegacyKeys = options.hasLegacyKeys ?? hasLegacySilkscreenKeys;
  const finishSetup = options.finishSetup ?? invokeSetupFinish;
  const listenSetupChanged = options.listenSetupChanged ?? listenSetupChangedEvent;

  const cache = new Map<SettingKey, unknown>();
  const listeners = new Map<SettingKey, Set<(value: never) => void>>();
  let backendPromise: Promise<SettingsBackend> | null = null;
  let initPromise: Promise<void> | null = null;
  let storageListenerInstalled = false;

  const resolveBackend = (): Promise<SettingsBackend> => {
    if (!backendPromise) backendPromise = Promise.resolve(backend);
    return backendPromise;
  };

  function readMirror<K extends SettingKey>(key: K): SettingsSchema[K] | undefined {
    return parseMirror(key, safeLocalStorage.getItem(mirrorKey(key)));
  }

  function writeMirror<K extends SettingKey>(key: K, value: SettingsSchema[K]): void {
    safeLocalStorage.setItem(mirrorKey(key), JSON.stringify(value));
  }

  function getSetting<K extends SettingKey>(key: K): SettingsSchema[K] {
    if (cache.has(key)) return cache.get(key) as SettingsSchema[K];
    const mirrored = readMirror(key);
    if (mirrored !== undefined) {
      cache.set(key, mirrored);
      return mirrored;
    }
    return SETTING_DEFAULTS[key];
  }

  function notify<K extends SettingKey>(key: K, value: SettingsSchema[K]): void {
    const set = listeners.get(key);
    if (!set) return;
    for (const cb of [...set]) {
      try {
        (cb as (v: SettingsSchema[K]) => void)(value);
      } catch (error) {
        console.warn(`[kaleo settings] a subscriber to ${key} threw:`, error);
      }
    }
  }

  /**
   * Put a value in the cache and tell subscribers, once, if it differs from
   * what they last heard. Every channel — a local set, the backend's change
   * event, the mirror's storage event, a hydrate — lands here.
   */
  function accept<K extends SettingKey>(key: K, value: SettingsSchema[K], mirror: boolean): boolean {
    const previous = cache.has(key) ? cache.get(key) : undefined;
    const changed = !cache.has(key) || !Object.is(previous, value);
    cache.set(key, value);
    if (mirror) writeMirror(key, value);
    if (changed) notify(key, value);
    return changed;
  }

  function onStorage(event: StorageEvent): void {
    // `key === null` is `localStorage.clear()` in another window; nothing to
    // read from it, and the cache stays authoritative until the next hydrate.
    const key = settingOfMirrorKey(event.key);
    if (!key) return;
    const value = parseMirror(key, event.newValue);
    if (value === undefined) return;
    accept(key, value, false);
  }

  function installStorageListener(): void {
    if (storageListenerInstalled || typeof window === "undefined") return;
    storageListenerInstalled = true;
    try {
      window.addEventListener("storage", onStorage);
    } catch {
      storageListenerInstalled = false;
    }
  }

  function subscribe<K extends SettingKey>(
    key: K,
    cb: (value: SettingsSchema[K]) => void
  ): () => void {
    installStorageListener();
    let set = listeners.get(key);
    if (!set) {
      set = new Set();
      listeners.set(key, set);
    }
    set.add(cb as (value: never) => void);
    return () => {
      set?.delete(cb as (value: never) => void);
    };
  }

  async function setSetting<K extends SettingKey>(key: K, value: SettingsSchema[K]): Promise<void> {
    if (!isValidSetting(key, value)) {
      throw new TypeError(`[kaleo settings] refusing to store an invalid value for ${key}`);
    }
    // Cache and mirror first, so a synchronous read straight after the call
    // already sees the new value and the other window hears it now rather
    // than after the plugin round trip.
    accept(key, value, true);
    try {
      await (await resolveBackend()).set(key, value);
    } catch (error) {
      console.warn(`[kaleo settings] could not persist ${key}:`, error);
    }
  }

  async function hydrate(): Promise<void> {
    let loaded: Partial<Record<SettingKey, unknown>>;
    try {
      loaded = await (await resolveBackend()).load();
    } catch (error) {
      console.warn("[kaleo settings] could not read the settings store:", error);
      return;
    }
    for (const key of SETTING_KEYS) {
      const value = loaded[key];
      if (value === undefined) continue;
      if (!isValidSetting(key, value)) {
        console.warn(`[kaleo settings] ignoring an invalid stored value for ${key}`);
        continue;
      }
      accept(key, value, true);
    }
  }

  async function init(): Promise<void> {
    installStorageListener();
    let loaded: Partial<Record<SettingKey, unknown>> = {};
    let backendReady = false;
    try {
      loaded = await (await resolveBackend()).load();
      backendReady = true;
    } catch (error) {
      console.warn("[kaleo settings] could not read the settings store:", error);
    }
    for (const key of SETTING_KEYS) {
      const value = loaded[key];
      if (value !== undefined && isValidSetting(key, value)) accept(key, value, true);
    }

    // Existing-user detection. A build before the store existed left
    // `silkscreen_*` keys behind; someone who has already used Kaleo must not
    // be gated behind a first-launch assistant, so setup is recorded as done
    // at version 0 (below SETUP_VERSION, which is the "Run Setup Again" hint)
    // and the Rust side is told to reveal the strip. Only when the backend
    // actually answered: a read failure says nothing about whether the flag
    // exists, and writing `completed` over it could hide the assistant from
    // a fresh install.
    if (backendReady && loaded["setup.completed"] === undefined && hasLegacyKeys()) {
      await setSetting("setup.completed", true);
      await setSetting("setup.version", 0);
      if (tauri) {
        try {
          await finishSetup([]);
        } catch (error) {
          // The command may not exist until the Rust half lands; the flag is
          // in the store either way, and the next launch reads it from there.
          console.warn("[kaleo settings] setup_finish was not accepted:", error);
        }
      }
    }

    if (backendReady) {
      try {
        const be = await resolveBackend();
        await be.onChange?.((key, value) => {
          if (!SETTING_KEYS.includes(key as SettingKey)) return;
          const k = key as SettingKey;
          if (value === undefined || !isValidSetting(k, value)) return;
          accept(k, value, true);
        });
      } catch (error) {
        console.warn("[kaleo settings] store change events unavailable:", error);
      }
    }

    if (tauri) {
      try {
        await listenSetupChanged(() => void hydrate());
      } catch (error) {
        console.warn(`[kaleo settings] could not listen for ${SETUP_CHANGED_EVENT}:`, error);
      }
    }
  }

  function initSettings(): Promise<void> {
    if (!initPromise) initPromise = init();
    return initPromise;
  }

  return { getSetting, setSetting, subscribe, initSettings, hydrate };
}

// ------------------------------------------------------------- the singleton

const defaultStore: SettingsApi = createSettingsStore(
  isTauriRuntime() ? tauriBackend() : memoryBackend()
);

/** Synchronous read: cache, then the localStorage mirror, then the default. */
export function getSetting<K extends SettingKey>(key: K): SettingsSchema[K] {
  return defaultStore.getSetting(key);
}

/** Write cache + mirror now, persist through the plugin; resolves when persisted (or gave up). */
export function setSetting<K extends SettingKey>(key: K, value: SettingsSchema[K]): Promise<void> {
  return defaultStore.setSetting(key, value);
}

/** Hear every change to one key, from this window or the other, once per change. */
export function subscribe<K extends SettingKey>(
  key: K,
  cb: (value: SettingsSchema[K]) => void
): () => void {
  return defaultStore.subscribe(key, cb);
}

/**
 * Hydrate the cache from disk and run the one-time existing-user check.
 * Idempotent; `main.tsx` calls it without awaiting before the first render,
 * and the mirror keeps first paint honest in the meantime.
 */
export function initSettings(): Promise<void> {
  return defaultStore.initSettings();
}

/** Re-read the store (the Rust side wrote `setup.*` itself). */
export function hydrateSettings(): Promise<void> {
  return defaultStore.hydrate();
}
