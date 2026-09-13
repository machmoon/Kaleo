// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import {
  DEFAULT_SKIN,
  SKINS,
  getSkin,
  getTerminalAda,
  isSkinId,
  setSkin,
  setTerminalAda,
  skinInfo,
} from "./overlay-skin";

beforeEach(() => localStorage.clear());

describe("the skin catalogue", () => {
  it("has no duplicate ids, so a stored choice is unambiguous", () => {
    expect(new Set(SKINS.map((s) => s.id)).size).toBe(SKINS.length);
  });

  it("names a source for every skin", () => {
    // Each shape came from reading a real implementation; a skin with no
    // source is one somebody invented in a mock and nobody checked.
    for (const skin of SKINS) expect(skin.source.length).toBeGreaterThan(0);
  });

  it("defaults to the bar this app already was", () => {
    // A fork that silently changes the overlay on upgrade is a bug report.
    expect(DEFAULT_SKIN).toBe("plain");
    expect(getSkin()).toBe("plain");
  });
});

describe("the stored choice", () => {
  it("round-trips", () => {
    setSkin("terminal");
    expect(getSkin()).toBe("terminal");
  });

  it("falls back rather than leaving a blank overlay", () => {
    // The real case: someone tries a skin, then downgrades to a build that
    // no longer has it. Throwing here means no overlay and no way to reach
    // settings to fix it.
    localStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, "hologram");
    expect(getSkin()).toBe(DEFAULT_SKIN);
    expect(skinInfo("hologram" as never).id).toBe(DEFAULT_SKIN);
  });

  it("rejects non-strings without throwing", () => {
    for (const value of [null, undefined, 42, {}, []]) {
      expect(isSkinId(value)).toBe(false);
    }
  });
});

describe("the terminal's Ada switch", () => {
  it("is on by default, because that is the point of the skin", () => {
    expect(getTerminalAda()).toBe(true);
  });

  it("turns the skin into an ordinary shell when switched off", () => {
    setTerminalAda(false);
    expect(getTerminalAda()).toBe(false);
    setTerminalAda(true);
    expect(getTerminalAda()).toBe(true);
  });
});

describe("honesty about what exists", () => {
  it("tracks which skins the overlay actually renders", () => {
    // Offering options that silently do nothing is the bug this flag exists
    // to prevent. All four render now; the assertion stays so a fifth skin
    // cannot be listed before it is wired.
    const built = SKINS.filter((s) => s.built).map((s) => s.id);
    expect(built).toEqual(["plain", "spotlight", "orb", "terminal"]);
  });

  it("still stores an unbuilt choice, so the preference survives", () => {
    setSkin("orb");
    expect(getSkin()).toBe("orb");
  });
});
