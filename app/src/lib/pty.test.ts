import { describe, expect, it } from "vitest";
import { decodeBytes, encodeBytes } from "./pty";

describe("pty byte transport", () => {
  it("round-trips arbitrary bytes, including invalid UTF-8", () => {
    // A paste can contain anything, and terminal output certainly can.
    const bytes = new Uint8Array([0x00, 0x1b, 0x5b, 0x41, 0xff, 0xfe, 0x80, 0x7f]);
    expect(decodeBytes(encodeBytes(bytes))).toEqual(bytes);
  });

  it("survives a multi-byte character split across two chunks", () => {
    // The exact bug a String round trip introduces: half of a codepoint in
    // one read, half in the next, and U+FFFD forever after.
    const emoji = new TextEncoder().encode("héllo ✅ 🛠");
    const first = emoji.subarray(0, 8);
    const second = emoji.subarray(8);
    const rejoined = new Uint8Array([...decodeBytes(encodeBytes(first)), ...decodeBytes(encodeBytes(second))]);
    expect(new TextDecoder().decode(rejoined)).toBe("héllo ✅ 🛠");
  });

  it("encodes a large paste without blowing the call stack", () => {
    // `String.fromCharCode(...bytes)` on a megabyte throws; hence chunking.
    const big = new Uint8Array(1_000_000).fill(0x61);
    expect(decodeBytes(encodeBytes(big)).length).toBe(1_000_000);
  });
});
