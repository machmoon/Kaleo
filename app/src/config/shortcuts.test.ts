// @vitest-environment jsdom
//
// Every advertised shortcut must do something.
//
// The Shortcuts page draws `DEFAULT_SHORTCUT_ACTIONS`, and `update_shortcuts`
// registers each one as a system-wide hotkey. Whether pressing it does
// anything is decided in Rust, by the match in `handle_shortcut_action`
// (src-tauri/src/shortcuts.rs). The two lists drifted once: three
// Pluely-era actions (voice recording, system audio, screenshot) stayed in
// the config after their handlers emitted events nothing listened to, so the
// page advertised key combos that stole the keys and did nothing. This test
// reads the Rust source and refuses a config id with no arm.

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { DEFAULT_SHORTCUT_ACTIONS } from "./shortcuts";
import { STORAGE_KEYS } from "./constants";

// `__dirname`, not `import.meta.url`: under the jsdom environment the
// latter is an http: URL and `readFileSync` refuses it.
const shortcutsRs = readFileSync(
  path.resolve(__dirname, "../../src-tauri/src/shortcuts.rs"),
  "utf8"
);

/** The literal arms of `handle_shortcut_action`'s match. */
function handledActionIds(): Set<string> {
  const start = shortcutsRs.indexOf("pub fn handle_shortcut_action");
  expect(start).toBeGreaterThan(-1);
  const body = shortcutsRs.slice(start, shortcutsRs.indexOf("\n}\n", start));
  return new Set(
    [...body.matchAll(/^\s*"([a-z_]+)"\s*=>/gm)].map((m) => m[1])
  );
}

describe("DEFAULT_SHORTCUT_ACTIONS", () => {
  it("names only actions the Rust handler has an arm for", () => {
    const handled = handledActionIds();
    for (const action of DEFAULT_SHORTCUT_ACTIONS) {
      // `move_window` is registered as four arrow-key bindings, one per
      // direction, and handled as `move_window_<dir>`.
      const ids =
        action.id === "move_window"
          ? ["up", "down", "left", "right"].map((d) => `move_window_${d}`)
          : [action.id];
      for (const id of ids) {
        expect(handled, `${action.id} has no handler arm`).toContain(id);
      }
    }
  });

  it("no longer advertises the Pluely actions whose features are gone", () => {
    const ids = DEFAULT_SHORTCUT_ACTIONS.map((a) => a.id);
    expect(ids).not.toContain("audio_recording");
    expect(ids).not.toContain("system_audio");
    expect(ids).not.toContain("screenshot");
    // ... and the Rust side does not keep handlers for them either, which
    // would be an event with no listener.
    const handled = handledActionIds();
    expect(handled).not.toContain("audio_recording");
    expect(handled).not.toContain("system_audio");
    expect(shortcutsRs).not.toContain('"start-audio-recording"');
    expect(shortcutsRs).not.toContain('"toggle-system-audio"');
  });
});

describe("getShortcutsConfig", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetModules();
  });

  it("drops a stored binding for an action that no longer exists", async () => {
    localStorage.setItem(
      STORAGE_KEYS.SHORTCUTS,
      JSON.stringify({
        bindings: {
          audio_recording: {
            action: "audio_recording",
            key: "cmd+shift+a",
            enabled: true,
          },
          toggle_window: {
            action: "toggle_window",
            key: "cmd+shift+k",
            enabled: true,
          },
        },
      })
    );
    const { getShortcutsConfig } = await import("@/lib/storage/shortcuts.storage");
    const config = getShortcutsConfig();
    expect(config.bindings.audio_recording).toBeUndefined();
    // A stored binding for a live action still wins over the default.
    expect(config.bindings.toggle_window?.key).toBe("cmd+shift+k");
    // Every default is still present.
    for (const action of DEFAULT_SHORTCUT_ACTIONS) {
      expect(config.bindings[action.id]).toBeDefined();
    }
  });

  it("keeps a binding for a stored custom action", async () => {
    localStorage.setItem(
      STORAGE_KEYS.SHORTCUTS,
      JSON.stringify({
        bindings: {
          mine: { action: "mine", key: "cmd+shift+9", enabled: true },
        },
        customActions: [
          {
            id: "mine",
            name: "Mine",
            description: "",
            defaultKey: { macos: "cmd+shift+9", windows: "", linux: "" },
          },
        ],
      })
    );
    const { getShortcutsConfig } = await import("@/lib/storage/shortcuts.storage");
    expect(getShortcutsConfig().bindings.mine?.key).toBe("cmd+shift+9");
  });
});
