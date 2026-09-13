/**
 * The typed seam over the real pty in `src-tauri/src/pty.rs`.
 *
 * Two things this module exists to keep honest:
 *
 * - **Bytes stay bytes.** Output crosses as base64 and is handed to xterm.js
 *   as a `Uint8Array`. A `String` round trip would corrupt any multi-byte
 *   character that happened to straddle a read boundary, permanently, for no
 *   reason other than buffer size. xterm.js decodes statefully and handles
 *   the split.
 * - **Output arrives on a `Channel`, not the event bus.** Tauri's docs say
 *   the event system is not built for low latency or high throughput and may
 *   deliver out of order; out-of-order terminal output is scrambled bytes.
 */

import { Channel, invoke } from "@tauri-apps/api/core";

export type PtyEvent = { kind: "output"; b64: string } | { kind: "exit"; code: number };

export interface PtyInfo {
  id: string;
  shell: string;
  cols: number;
  rows: number;
}

export interface OpenOptions {
  id: string;
  cols: number;
  rows: number;
  cwd?: string;
  shell?: string;
}

const B64_CHUNK = 0x8000;

/** base64 of arbitrary bytes, chunked so a large paste cannot blow the stack. */
export function encodeBytes(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += B64_CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + B64_CHUNK));
  }
  return btoa(binary);
}

export function decodeBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

/** Open a shell on a pty. `onEvent` fires for every chunk and for the exit. */
export async function openPty(
  options: OpenOptions,
  onEvent: (event: PtyEvent) => void,
): Promise<PtyInfo> {
  const channel = new Channel<PtyEvent>();
  channel.onmessage = onEvent;
  return invoke<PtyInfo>("pty_open", {
    id: options.id,
    cols: Math.max(2, Math.round(options.cols)),
    rows: Math.max(1, Math.round(options.rows)),
    cwd: options.cwd ?? null,
    shell: options.shell ?? null,
    onOutput: channel,
  });
}

/** Send raw keystrokes. */
export async function writePty(id: string, bytes: Uint8Array): Promise<void> {
  await invoke("pty_write", { id, b64: encodeBytes(bytes) });
}

const ENCODER = new TextEncoder();

/** Send text. Used for a submitted line; `\r` is the shell's Enter, not `\n`. */
export async function writeText(id: string, text: string): Promise<void> {
  await writePty(id, ENCODER.encode(text));
}

/**
 * Tell the pty its new size.
 *
 * Skipping this is the single most common bug in terminal-in-Tauri projects:
 * everything looks right until the first `vim`, which then draws into a 24x80
 * box in the corner of a much larger window.
 */
export async function resizePty(id: string, cols: number, rows: number): Promise<void> {
  await invoke("pty_resize", {
    id,
    cols: Math.max(2, Math.round(cols)),
    rows: Math.max(1, Math.round(rows)),
  });
}

/** Close and kill. Safe to call twice — an unmount can run twice. */
export async function closePty(id: string): Promise<boolean> {
  return invoke<boolean>("pty_close", { id });
}

export async function listPtySessions(): Promise<string[]> {
  return invoke<string[]>("pty_sessions");
}
