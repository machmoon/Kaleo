// Config-level guards for two properties this app cannot assert from its own
// code: that the webview is allowed to reach the engine at all, and that its
// windows can be recorded.
//
// These read the shipped files rather than a copy, because the defect they
// guard against is someone loosening or mistyping the real config.

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const readJson = (relative: string) =>
  JSON.parse(readFileSync(new URL(relative, import.meta.url), "utf8"));

// A URL pattern Tauri cannot tokenize does not merely fail to match -- it makes
// the whole scope fail to deserialize, so *every* request the app issues is
// rejected before it reaches the network. An IPv6 literal does exactly that,
// because `:` opens a named group in the pattern syntax and `[::1]` leaves one
// unnamed. This has shipped twice now, once as a `https://**` glob and once as
// `http://[::1]:*`, and it presents as "could not reach the silkscreen engine"
// with no request in the engine's log and no error in the app's -- health()
// swallows its errors behind a status dot, and the plugin rejects with a bare
// string, so `error.message` reads `undefined`.
describe("http capability scope", () => {
  const capabilities = ["default", "cross-platform"].map((name) => ({
    name,
    json: readJson(`../../src-tauri/capabilities/${name}.json`),
  }));

  const httpScopeUrls = (json: {
    permissions: (string | { identifier: string; allow?: { url?: string }[] })[];
  }): string[] =>
    json.permissions
      .filter(
        (p): p is { identifier: string; allow?: { url?: string }[] } =>
          typeof p === "object" && p.identifier === "http:default"
      )
      .flatMap((p) => (p.allow ?? []).map((e) => e.url ?? ""));

  it.each(capabilities)("$name declares an http scope", ({ json }) => {
    expect(httpScopeUrls(json).length).toBeGreaterThan(0);
  });

  it.each(capabilities)("$name has no IPv6 literal", ({ json }) => {
    for (const url of httpScopeUrls(json)) {
      expect(url).not.toMatch(/\[|\]/);
    }
  });

  // The scope mirrors what the Engine page accepts
  // (pages/engine/components/EngineConnection.tsx, validateEngineBaseUrl):
  // plain http only on loopback, because the bearer token would cross the
  // network in the clear; and any https origin, because a deployed engine
  // sits behind that token and the app keeps no allowlist of deployed hosts.
  // Until 2026-09-06 the scope was loopback-only while the page already
  // accepted https, so a saved Cloud Run address validated, stored, and then
  // had every request refused by the plugin -- the silent failure described
  // above, from the other direction.
  it.each(capabilities)("$name allows plain http only on loopback", ({ json }) => {
    for (const url of httpScopeUrls(json)) {
      if (url.startsWith("http://")) {
        expect(url).toMatch(/^http:\/\/(127\.0\.0\.1|localhost):\*/);
      }
    }
  });

  it.each(capabilities)("$name reaches a deployed https engine", ({ json }) => {
    const urls = httpScopeUrls(json);
    // Both forms, as for loopback: the pattern without a port matches only
    // the default 443, and a deployed engine may sit on another port.
    expect(urls).toContain("https://*");
    expect(urls).toContain("https://*/*");
    expect(urls).toContain("https://*:*");
    expect(urls).toContain("https://*:*/*");
    // Never the `https://**` glob the comment above records as having shipped.
    for (const url of urls) expect(url).not.toMatch(/\*\*/);
  });

  it("both platform files declare the same scope", () => {
    const [a, b] = capabilities.map((c) => httpScopeUrls(c.json).sort());
    expect(a).toEqual(b);
  });
});

// Upstream excluded its windows from screen capture so they stayed hidden
// during a call. This app is demoed and screen-shared, and a protected window
// records as a blank rectangle -- the board, schematic and review would be
// missing from exactly the video meant to show them.
describe("screen capture", () => {
  it("leaves the dashboard capturable", () => {
    const windowRs = readFileSync(
      new URL("../../src-tauri/src/window.rs", import.meta.url),
      "utf8"
    );
    expect(windowRs).not.toMatch(/\.content_protected\(true\)/);
  });

  it("does not protect the overlay either", () => {
    const config = readJson("../../src-tauri/tauri.conf.json");
    expect(config.app.windows[0].contentProtected).toBe(false);
  });
});

// The menu bar icon (src-tauri/src/tray.rs) is built and driven from Rust; the
// webview's only part in it is one `invoke("tray_set_state")` and one
// `listen("tray-ada-toggle")`. So the capability files need the event and
// nothing more: no `core:tray:*` or `core:menu:*` grant, because no JS calls
// the tray or menu API and a grant nothing uses is a grant something could
// misuse. The Cargo features are pinned too, since without `tray-icon` the
// module does not compile and without `image-png` its icons do not decode.
describe("menu bar tray", () => {
  const capabilities = ["default", "cross-platform"].map((name) => ({
    name,
    json: readJson(`../../src-tauri/capabilities/${name}.json`),
  }));

  type Permission = string | { identifier: string; allow?: { event?: string }[] };
  const listenEvents = (json: { permissions: Permission[] }): string[] =>
    json.permissions
      .filter(
        (p): p is { identifier: string; allow?: { event?: string }[] } =>
          typeof p === "object" && p.identifier === "core:event:allow-listen"
      )
      .flatMap((p) => (p.allow ?? []).map((e) => e.event ?? ""));
  const identifiers = (json: { permissions: Permission[] }): string[] =>
    json.permissions.map((p) => (typeof p === "string" ? p : p.identifier));

  it.each(capabilities)("$name lets the overlay hear the tray's toggle", ({ json }) => {
    expect(listenEvents(json)).toContain("tray-ada-toggle");
  });

  it.each(capabilities)("$name grants no JS tray or menu API", ({ json }) => {
    for (const id of identifiers(json)) {
      expect(id).not.toMatch(/^core:(tray|menu)/);
    }
  });

  it("enables the Cargo features the module needs", () => {
    const cargo = readFileSync(
      new URL("../../src-tauri/Cargo.toml", import.meta.url),
      "utf8"
    );
    const tauriDep = cargo.match(/^tauri = \{[^\n]*\}/m)?.[0] ?? "";
    expect(tauriDep).toContain('"tray-icon"');
    expect(tauriDep).toContain('"image-png"');
  });

  it("ships both mic-state glyphs for every platform", () => {
    for (const file of [
      "idleTemplate@2x.png",
      "liveTemplate@2x.png",
      "idle-colour.png",
      "live-colour.png",
    ]) {
      const bytes = readFileSync(new URL(`../../src-tauri/icons/tray/${file}`, import.meta.url));
      // A PNG, not an empty placeholder.
      expect(bytes.subarray(1, 4).toString("latin1")).toBe("PNG");
    }
  });
});

// The Setup Assistant's two plugins (src-tauri/src/setup.rs). Each guard
// below pins a decision the plan froze and nothing in the app's own code can
// assert: the capability files grant exactly the commands the settings store
// and the notification step use and nothing wider (never a `:default` set,
// which would carry every command the plugin ships); both webviews can hear
// the setup-changed event; the two crates are registered where Rust reads
// the store before any webview boots; banners go through the plugin rather
// than the Web Notification API, which a WKWebView never shows; and only one
// store file is ever loaded, because the Rust gate reads that exact path.
describe("setup assistant plugins", () => {
  const capabilities = ["default", "cross-platform"].map((name) => ({
    name,
    json: readJson(`../../src-tauri/capabilities/${name}.json`) as {
      permissions: (string | { identifier: string; allow?: { event?: string }[] })[];
    },
  }));

  const NOTIFICATION_ALLOWS = [
    "notification:allow-notify",
    "notification:allow-is-permission-granted",
    "notification:allow-request-permission",
  ];
  const STORE_ALLOWS = ["store:allow-load", "store:allow-get", "store:allow-set"];

  const identifiers = (json: (typeof capabilities)[number]["json"]) =>
    json.permissions.map((p) => (typeof p === "string" ? p : p.identifier));

  it.each(capabilities)("$name grants exactly the three notification commands", ({ json }) => {
    const ids = identifiers(json).filter((id) => id.startsWith("notification:"));
    expect(ids.sort()).toEqual([...NOTIFICATION_ALLOWS].sort());
    expect(ids).not.toContain("notification:default");
  });

  it.each(capabilities)("$name grants only the three store commands", ({ json }) => {
    const ids = identifiers(json).filter((id) => id.startsWith("store:"));
    expect(ids.length).toBeGreaterThan(0);
    for (const id of ids) expect(STORE_ALLOWS).toContain(id);
    expect(ids).not.toContain("store:default");
  });

  it.each(capabilities)("$name lets both windows listen for kaleo-setup-changed", ({ json }) => {
    const events = json.permissions
      .filter(
        (p): p is { identifier: string; allow?: { event?: string }[] } =>
          typeof p === "object" && p.identifier === "core:event:allow-listen"
      )
      .flatMap((p) => (p.allow ?? []).map((e) => e.event));
    expect(events).toContain("kaleo-setup-changed");
  });

  it("Cargo.toml depends on both plugins and lib.rs registers both", () => {
    const cargo = readFileSync(new URL("../../src-tauri/Cargo.toml", import.meta.url), "utf8");
    expect(cargo).toMatch(/^tauri-plugin-store\s*=/m);
    expect(cargo).toMatch(/^tauri-plugin-notification\s*=/m);
    const libRs = readFileSync(new URL("../../src-tauri/src/lib.rs", import.meta.url), "utf8");
    expect(libRs).toMatch(/\.plugin\(tauri_plugin_store::Builder::new\(\)\.build\(\)\)/);
    expect(libRs).toMatch(/\.plugin\(tauri_plugin_notification::init\(\)\)/);
  });

  // Every .ts/.tsx under src/, this file included; the literal it hunts for
  // is assembled so this file cannot match itself.
  const srcDir = fileURLToPath(new URL("..", import.meta.url));
  const sources = readdirSync(srcDir, { recursive: true, encoding: "utf8" })
    .filter((rel) => /\.(ts|tsx)$/.test(rel))
    .map((rel) => ({ rel, text: readFileSync(join(srcDir, rel), "utf8") }));

  it("never constructs a Web Notification", () => {
    const forbidden = new RegExp("new\\s+" + "Notification" + "\\(");
    for (const { rel, text } of sources) {
      expect(text, rel).not.toMatch(forbidden);
    }
  });

  it("only ever loads settings.json from the store plugin", () => {
    // The Rust gate reads ONE path (`setup.rs::STORE_FILE`) before any webview
    // boots, and the store plugin caches a Store per path while ignoring the
    // options of every later `load`. So a second path here would mean the
    // wizard's writes went somewhere Rust never reads, and a JS-first load of
    // the same path with different options would leave them in memory.
    //
    // The call passes the `SETTINGS_STORE_FILE` constant rather than a literal,
    // so this checks both halves: the constant's value, and that nothing calls
    // load with anything else.
    const users = sources.filter(({ text }) => text.includes("@tauri-apps/plugin-store"));
    expect(users.length).toBeGreaterThan(0);

    const keys = readFileSync(fileURLToPath(new URL("../lib/settings/keys.ts", import.meta.url)), "utf8");
    expect(keys).toMatch(/SETTINGS_STORE_FILE\s*=\s*"settings\.json"/);

    const rust = readFileSync(
      fileURLToPath(new URL("../../src-tauri/src/setup.rs", import.meta.url)),
      "utf8",
    );
    expect(rust).toMatch(/STORE_FILE:\s*&str\s*=\s*"settings\.json"/);

    const loadCall = /\b(?:Store\.load|LazyStore)\(\s*([^,)\s]+)/g;
    let calls = 0;
    for (const { rel, text } of users) {
      for (const match of text.matchAll(loadCall)) {
        calls += 1;
        expect(match[1], `${rel}: ${match[0]}`).toBe("SETTINGS_STORE_FILE");
      }
    }
    expect(calls).toBeGreaterThan(0);
  });
});
