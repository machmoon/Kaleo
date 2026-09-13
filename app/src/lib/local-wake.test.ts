import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(),
}));

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  LOCAL_WAKE_EVENT,
  describeLocalWake,
  localWakeStatus,
  startLocalWake,
  stopLocalWake,
  subscribeLocalWake,
  unavailableLocalWake,
} from "./local-wake";

const mockListen = vi.mocked(listen);
const mockInvoke = vi.mocked(invoke);

beforeEach(() => {
  mockListen.mockReset();
  mockInvoke.mockReset();
});

describe("describeLocalWake", () => {
  it("is honest when the spotter is missing — PTT is the path", () => {
    expect(describeLocalWake(false)).toMatch(/mic button/);
    expect(describeLocalWake(false)).not.toMatch(/Gemini|always-on|Siri/i);
    expect(describeLocalWake(true)).toMatch(/on-device/);
  });
});

describe("subscribeLocalWake", () => {
  it("subscribes to the Rust event name siblings emit", async () => {
    const unlisten = vi.fn();
    mockListen.mockResolvedValue(unlisten);
    const onHit = vi.fn();
    const stop = await subscribeLocalWake(onHit);
    expect(mockListen).toHaveBeenCalledWith(LOCAL_WAKE_EVENT, expect.any(Function));
    expect(stop).toBe(unlisten);
  });

  it("returns null when Tauri events are missing so PTT can stay primary", async () => {
    mockListen.mockRejectedValue(new Error("not in tauri"));
    expect(await subscribeLocalWake(() => {})).toBeNull();
  });
});

describe("localWakeStatus", () => {
  it("returns the Rust snapshot when the command exists", async () => {
    mockInvoke.mockResolvedValue({
      available: true,
      listening: false,
      backend: "porcupine",
      reason: "ready",
    });
    expect(await localWakeStatus()).toMatchObject({
      available: true,
      backend: "porcupine",
    });
    expect(mockInvoke).toHaveBeenCalledWith("wake_status");
  });

  it("falls back to PTT when invoke is missing", async () => {
    mockInvoke.mockRejectedValue(new Error("not in tauri"));
    expect(await localWakeStatus()).toEqual(unavailableLocalWake());
  });
});

describe("start/stop local wake", () => {
  it("invoke the Rust commands", async () => {
    mockInvoke.mockResolvedValue({
      available: true,
      listening: true,
      backend: "mock",
      reason: "",
    });
    await startLocalWake();
    expect(mockInvoke).toHaveBeenCalledWith("wake_start");
    await stopLocalWake();
    expect(mockInvoke).toHaveBeenCalledWith("wake_stop");
  });
});
