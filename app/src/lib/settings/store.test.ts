// @vitest-environment jsdom
//
// The settings store over the memory backend: what a synchronous read sees
// before and after hydration, that a subscriber hears each change exactly
// once whichever channel carried it, that the mirror is written, and the two
// launch-time decisions — no plugin outside Tauri, and an existing user is
// never gated.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import {
  SETTING_DEFAULTS,
  SETTINGS_STORE_FILE,
  SETUP_VERSION,
  createSettingsStore,
  hasLegacySilkscreenKeys,
  isTauriRuntime,
  memoryBackend,
  type SettingsBackend,
} from "./store";

beforeEach(() => {
  window.localStorage.clear();
});

function build(
  initial: Parameters<typeof memoryBackend>[0] = {},
  overrides: Parameters<typeof createSettingsStore>[1] = {}
) {
  const backend = memoryBackend(initial);
  const api = createSettingsStore(backend, {
    tauri: false,
    hasLegacyKeys: () => false,
    ...overrides,
  });
  return { api, backend };
}

describe("defaults", () => {
  it("answers every key with its default on a fresh install, before and after init", async () => {
    const { api } = build();
    expect(api.getSetting("notify.enabled")).toBe(false);
    expect(api.getSetting("notify.os")).toBe("not_focused");
    expect(api.getSetting("setup.completed")).toBe(false);
    expect(api.getSetting("setup.version")).toBe(0);
    expect(api.getSetting("setup.remaining")).toEqual([]);
    expect(api.getSetting("setup.skipped")).toEqual([]);
    expect(api.getSetting("tour.completed")).toBe(false);
    await api.initSettings();
    expect(api.getSetting("setup.completed")).toBe(false);
    expect(api.getSetting("notify.os")).toBe(SETTING_DEFAULTS["notify.os"]);
  });

  it("only ever names settings.json as the store file", () => {
    expect(SETTINGS_STORE_FILE).toBe("settings.json");
    expect(SETUP_VERSION).toBe(1);
  });
});

describe("set then get", () => {
  it("reads back synchronously, and persists to the backend", async () => {
    const { api, backend } = build();
    const pending = api.setSetting("notify.enabled", true);
    // Synchronous: the cache answers before the backend round trip resolves.
    expect(api.getSetting("notify.enabled")).toBe(true);
    await pending;
    expect((await backend.load())["notify.enabled"]).toBe(true);
  });

  it("hydrates what the backend already held", async () => {
    const { api } = build({ "setup.completed": true, "setup.skipped": ["google"] });
    await api.initSettings();
    expect(api.getSetting("setup.completed")).toBe(true);
    expect(api.getSetting("setup.skipped")).toEqual(["google"]);
  });

  it("refuses an invalid value rather than storing it", async () => {
    const { api } = build();
    await expect(
      api.setSetting("notify.os", "sometimes" as unknown as "always")
    ).rejects.toThrow(/invalid/);
    expect(api.getSetting("notify.os")).toBe("not_focused");
  });

  it("ignores an invalid stored value and keeps the default", async () => {
    const { api } = build({ "notify.os": "loudly", "setup.version": "1" });
    await api.initSettings();
    expect(api.getSetting("notify.os")).toBe("not_focused");
    expect(api.getSetting("setup.version")).toBe(0);
  });
});

describe("subscribe", () => {
  it("fires once per change, not per set, and not after unsubscribe", async () => {
    const { api } = build();
    const heard: boolean[] = [];
    const off = api.subscribe("notify.enabled", (v) => heard.push(v));
    await api.setSetting("notify.enabled", true);
    await api.setSetting("notify.enabled", true); // same value: silent
    await api.setSetting("notify.enabled", false);
    expect(heard).toEqual([true, false]);
    off();
    await api.setSetting("notify.enabled", true);
    expect(heard).toEqual([true, false]);
  });

  it("hears a change from the other window through the mirror's storage event", async () => {
    const { api } = build();
    const heard: string[] = [];
    api.subscribe("notify.os", (v) => heard.push(v));
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: KALEO_STORAGE_KEYS.NOTIFY_OS,
        newValue: JSON.stringify("always"),
      })
    );
    expect(heard).toEqual(["always"]);
    expect(api.getSetting("notify.os")).toBe("always");
    // The same value arriving again (the plugin's own event, say) is deduped.
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: KALEO_STORAGE_KEYS.NOTIFY_OS,
        newValue: JSON.stringify("always"),
      })
    );
    expect(heard).toEqual(["always"]);
  });

  it("hears a backend change event once, deduped against the cache", async () => {
    let emit: ((key: string, value: unknown) => void) | null = null;
    const backend: SettingsBackend = {
      ...memoryBackend(),
      async onChange(cb) {
        emit = cb;
        return () => {};
      },
    };
    const api = createSettingsStore(backend, { tauri: false, hasLegacyKeys: () => false });
    await api.initSettings();
    const heard: boolean[] = [];
    api.subscribe("tour.completed", (v) => heard.push(v));
    emit!("tour.completed", true);
    emit!("tour.completed", true);
    emit!("not.a.key", true);
    expect(heard).toEqual([true]);
    expect(api.getSetting("tour.completed")).toBe(true);
  });
});

describe("mirror", () => {
  it("writes every set as JSON under kaleo.settings.<key>", async () => {
    const { api } = build();
    await api.setSetting("notify.enabled", true);
    await api.setSetting("setup.remaining", ["stripe", "voice"]);
    expect(window.localStorage.getItem(KALEO_STORAGE_KEYS.NOTIFY_ENABLED)).toBe("true");
    expect(window.localStorage.getItem("kaleo.settings.setup.remaining")).toBe(
      JSON.stringify(["stripe", "voice"])
    );
  });

  it("answers from the mirror before init, so first paint sees the last value", () => {
    window.localStorage.setItem(KALEO_STORAGE_KEYS.SETUP_COMPLETED, "true");
    const { api } = build();
    expect(api.getSetting("setup.completed")).toBe(true);
  });

  it("never uses a silkscreen_ prefix, so the mirror cannot look like a legacy install", () => {
    expect(KALEO_STORAGE_KEYS.SETTINGS_MIRROR_PREFIX.startsWith("silkscreen_")).toBe(false);
    const { api } = build();
    void api.setSetting("notify.enabled", true);
    expect(hasLegacySilkscreenKeys()).toBe(false);
  });
});

describe("outside Tauri", () => {
  it("is not the Tauri runtime under Vitest", () => {
    expect("__TAURI_INTERNALS__" in window).toBe(false);
    expect(isTauriRuntime()).toBe(false);
  });

  it("never calls setup_finish or listens for the event when not in Tauri", async () => {
    const finishSetup = vi.fn(async () => {});
    const listenSetupChanged = vi.fn(async () => () => {});
    const { api } = build({}, { hasLegacyKeys: () => true, finishSetup, listenSetupChanged });
    await api.initSettings();
    expect(finishSetup).not.toHaveBeenCalled();
    expect(listenSetupChanged).not.toHaveBeenCalled();
  });
});

describe("existing-user detection", () => {
  it("marks setup complete at version 0 when silkscreen_ keys exist and the store has no flag", async () => {
    window.localStorage.setItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL, "http://127.0.0.1:8081");
    expect(hasLegacySilkscreenKeys()).toBe(true);
    const finishSetup = vi.fn(async () => {});
    const backend = memoryBackend();
    const api = createSettingsStore(backend, { tauri: true, finishSetup, listenSetupChanged: async () => () => {} });
    await api.initSettings();
    expect(api.getSetting("setup.completed")).toBe(true);
    expect(api.getSetting("setup.version")).toBe(0);
    expect((await backend.load())["setup.completed"]).toBe(true);
    expect(finishSetup).toHaveBeenCalledWith([]);
  });

  it("does not touch a store that already carries the flag, even a false one", async () => {
    window.localStorage.setItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL, "http://127.0.0.1:8081");
    const finishSetup = vi.fn(async () => {});
    const { api } = build(
      { "setup.completed": false },
      { tauri: true, hasLegacyKeys: hasLegacySilkscreenKeys, finishSetup, listenSetupChanged: async () => () => {} }
    );
    await api.initSettings();
    expect(api.getSetting("setup.completed")).toBe(false);
    expect(finishSetup).not.toHaveBeenCalled();
  });

  it("gates a fresh install: no legacy keys, no flag", async () => {
    const finishSetup = vi.fn(async () => {});
    const { api } = build({}, { tauri: true, hasLegacyKeys: hasLegacySilkscreenKeys, finishSetup, listenSetupChanged: async () => () => {} });
    await api.initSettings();
    expect(api.getSetting("setup.completed")).toBe(false);
    expect(finishSetup).not.toHaveBeenCalled();
  });

  it("survives setup_finish not existing yet", async () => {
    window.localStorage.setItem(KALEO_STORAGE_KEYS.ENGINE_BASE_URL, "http://127.0.0.1:8081");
    const finishSetup = vi.fn(async () => {
      throw new Error("Command setup_finish not found");
    });
    const { api } = build({}, { tauri: true, hasLegacyKeys: hasLegacySilkscreenKeys, finishSetup, listenSetupChanged: async () => () => {} });
    await expect(api.initSettings()).resolves.toBeUndefined();
    expect(api.getSetting("setup.completed")).toBe(true);
  });

  it("re-hydrates when the Rust side says setup changed", async () => {
    let fire: (() => void) | null = null;
    const backend = memoryBackend();
    const api = createSettingsStore(backend, {
      tauri: true,
      hasLegacyKeys: () => false,
      finishSetup: async () => {},
      listenSetupChanged: async (cb) => {
        fire = cb;
        return () => {};
      },
    });
    await api.initSettings();
    const heard: boolean[] = [];
    api.subscribe("setup.completed", (v) => heard.push(v));
    // The Rust side wrote straight to the store.
    await backend.set("setup.completed", true);
    await backend.set("setup.skipped", ["google", "stripe"]);
    fire!();
    await vi.waitFor(() => expect(heard).toEqual([true]));
    expect(api.getSetting("setup.skipped")).toEqual(["google", "stripe"]);
  });

  it("initSettings is idempotent", async () => {
    const load = vi.fn(async () => ({}));
    const backend: SettingsBackend = { load, set: async () => {} };
    const api = createSettingsStore(backend, { tauri: false, hasLegacyKeys: () => false });
    await Promise.all([api.initSettings(), api.initSettings()]);
    await api.initSettings();
    expect(load).toHaveBeenCalledTimes(1);
  });
});
