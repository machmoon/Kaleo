import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

import { invoke } from "@tauri-apps/api/core";
import {
  collectDeskCandidates,
  enrichWithDeskContext,
  needsDeskContext,
  shouldResolveDesk,
} from "./desk-context";
import { proceedAfterDesk } from "./desk-wake";

const mockInvoke = vi.mocked(invoke);

const SNAP = {
  png_base64: "abc",
  cursor_x: 100.4,
  cursor_y: 200.6,
  width: 1512,
  height: 982,
  error: "",
};

describe("needsDeskContext", () => {
  it("flags deixis and leaves free-standing board asks alone", () => {
    expect(needsDeskContext("I don't like this")).toBe(true);
    expect(needsDeskContext("what's this")).toBe(true);
    expect(needsDeskContext("move that over")).toBe(true);
    expect(needsDeskContext("fix it")).toBe(true);
    expect(needsDeskContext("make me a 3.3V LDO board")).toBe(false);
    expect(needsDeskContext("")).toBe(false);
  });
});

describe("shouldResolveDesk", () => {
  it("needs both deixis and a usable snap", () => {
    expect(shouldResolveDesk("what's this", SNAP)).toBe(true);
    expect(shouldResolveDesk("an LDO please", SNAP)).toBe(false);
    expect(shouldResolveDesk("what's this", null)).toBe(false);
    expect(
      shouldResolveDesk("what's this", { ...SNAP, error: "Screen Recording is off" })
    ).toBe(false);
    expect(shouldResolveDesk("what's this", { ...SNAP, png_base64: "" })).toBe(
      false
    );
  });
});

describe("proceedAfterDesk", () => {
  it("blocks a board start and lets a step command through", () => {
    expect(proceedAfterDesk({ kind: "start", intent: "what's this" })).toBe(
      false
    );
    expect(proceedAfterDesk({ kind: "command", text: "change this" })).toBe(
      true
    );
    expect(proceedAfterDesk({ kind: "listen" })).toBe(true);
    expect(proceedAfterDesk({ kind: "ignore", reason: "busy" })).toBe(true);
  });
});

describe("enrichWithDeskContext", () => {
  it("leaves non-deictic speech alone and never invents coordinates", async () => {
    expect(await enrichWithDeskContext("an LDO please")).toEqual({
      utterance: "an LDO please",
      snap: null,
    });
    expect(mockInvoke).not.toHaveBeenCalled();
  });

  it("returns the snap beside the original words and never claims a screenshot in text", async () => {
    mockInvoke.mockResolvedValue(SNAP);
    const out = await enrichWithDeskContext("I don't like this");
    expect(out.utterance).toBe("I don't like this");
    expect(out.snap).toEqual(SNAP);
    expect(out.utterance).not.toMatch(/screenshot/i);
    expect(out.utterance).not.toMatch(/\[desk:/);
  });

  it("keeps the utterance and a null snap when capture fails", async () => {
    mockInvoke.mockResolvedValue({
      png_base64: "",
      cursor_x: 0,
      cursor_y: 0,
      width: 0,
      height: 0,
      error: "Screen Recording is off",
    });
    expect(await enrichWithDeskContext("change this")).toEqual({
      utterance: "change this",
      snap: null,
    });
  });
});

describe("collectDeskCandidates", () => {
  it("reads testid plus identity attrs and skips duplicates", () => {
    const attrs = (map: Record<string, string | null>) => ({
      getAttribute: (name: string) => map[name] ?? null,
    });
    const root = {
      querySelectorAll: () => [
        attrs({ "data-testid": "finding-card", "data-sev": "blocker" }),
        attrs({ "data-testid": "finding-card", "data-sev": "blocker" }),
        attrs({ "data-testid": "board-well-part", "data-ref": "C1" }),
        attrs({ "data-testid": "prompt-input" }),
      ],
    } as unknown as ParentNode;
    expect(collectDeskCandidates(root)).toEqual([
      { testid: "finding-card", attrs: { sev: "blocker" } },
      { testid: "board-well-part", attrs: { ref: "C1" } },
      { testid: "prompt-input" },
    ]);
  });
});
