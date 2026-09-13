// Test support for the wizard: an in-memory stand-in for lane C's settings
// store, shaped like contract C1 (as amended). Tests mount it with
//
//   vi.mock("@/lib/settings/store", async () =>
//     (await import("@/lib/setup/testing")).makeSettingsStub());
//
// It lives here, not at `@/lib/settings/store`, because that path belongs to
// the real module. Nothing outside a test imports this file.

export type StubSettings = Record<string, unknown>;

export const STUB_DEFAULTS: StubSettings = {
  "notify.enabled": false,
  "notify.os": "not_focused",
  "setup.completed": false,
  "setup.version": 0,
  "setup.step": "hello",
  "setup.remaining": [],
  "setup.skipped": [],
  "setup.completedAt": 0,
  "tour.completed": false,
};

export function makeSettingsStub(initial: StubSettings = {}) {
  let values: StubSettings = { ...STUB_DEFAULTS, ...initial };
  const subs = new Map<string, Set<(v: unknown) => void>>();
  const api = {
    SETUP_VERSION: 1,
    SETUP_CHANGED_EVENT: "kaleo-setup-changed",
    isTauriRuntime: () => false,
    getSetting: (key: string) => values[key],
    setSetting: async (key: string, value: unknown) => {
      const before = values[key];
      values[key] = value;
      if (!Object.is(before, value)) for (const cb of subs.get(key) ?? []) cb(value);
    },
    subscribe: (key: string, cb: (v: unknown) => void) => {
      const set = subs.get(key) ?? new Set();
      set.add(cb);
      subs.set(key, set);
      return () => {
        set.delete(cb);
      };
    },
    initSettings: async () => undefined,
    /** Test-only: the whole map, to assert on. */
    __values: () => values,
    /** Test-only: back to defaults plus whatever the test seeds. */
    __reset: (next: StubSettings = {}) => {
      values = { ...STUB_DEFAULTS, ...next };
      subs.clear();
    },
  };
  return api;
}
