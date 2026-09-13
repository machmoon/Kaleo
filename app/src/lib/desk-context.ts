// Desk context for spoken deixis (“I don’t like this”).
//
// When the engineer points with the cursor and talks, Ada needs the screen
// under the pointer — not just the words. This module asks Rust for a
// screenshot (cursor burned in when the OS allows) plus cursor coordinates.
// The PNG rides a dedicated /desk/resolve call; it is never thrown away, and
// the utterance is never rewritten to claim a screenshot the pipeline cannot
// see. Prepending “[desk: … screenshot captured]” used to poison /generate
// via wakeAction → start. That path is closed: enrich returns the snap
// beside the original words.
//
// Full guided-cursor edits in KiCad are still unbuilt (docs/guided-cursor.md).
// This is the capture half: honest context, no fake click, no fake claim.

import { invoke } from "@tauri-apps/api/core";

export interface DeskSnapshot {
  /** PNG bytes, base64, no data-URL prefix. Empty when capture failed. */
  png_base64: string;
  cursor_x: number;
  cursor_y: number;
  width: number;
  height: number;
  /** Human reason when capture failed; empty on success. */
  error: string;
}

export interface DeskCandidate {
  testid: string;
  attrs?: Record<string, string>;
  tab?: string;
}

/** Original words plus the snap, or snap null when there is nothing to send. */
export interface DeskEnrichment {
  utterance: string;
  snap: DeskSnapshot | null;
}

/** Words that usually mean “what I’m pointing at”, not a free-standing noun. */
const DEICTIC =
  /\b(this|that|these|those|here|there|it)\b|\bdon'?t like\b|\bmove (this|that|it)\b|\bfix (this|that|it)\b|\bchange (this|that|it)\b/i;

/** Identity attrs the SPA guide already uses to tell repeated rows apart. */
const IDENTITY_ATTRS = ["ref", "sev", "selected", "parts", "step", "highlighted"] as const;

const MAX_CANDIDATES = 64;

export function needsDeskContext(utterance: string): boolean {
  const text = utterance.trim();
  if (!text) return false;
  return DEICTIC.test(text);
}

export function snapshotIsUsable(snap: DeskSnapshot | null): snap is DeskSnapshot {
  return Boolean(
    snap &&
      !snap.error &&
      snap.png_base64 &&
      snap.width > 0 &&
      snap.height > 0
  );
}

/** Deictic speech plus a real PNG: resolve on /desk/resolve, do not start a board. */
export function shouldResolveDesk(
  utterance: string,
  snap: DeskSnapshot | null
): boolean {
  return needsDeskContext(utterance) && snapshotIsUsable(snap);
}

export async function captureDeskSnapshot(): Promise<DeskSnapshot | null> {
  try {
    return await invoke<DeskSnapshot>("capture_desk_context");
  } catch {
    return null;
  }
}

/**
 * Live overlay controls the model may name as a target.
 *
 * Identity attributes only — never an index — matching the SPA `data-testid`
 * convention so a reordered list cannot silently rebind a pointer.
 */
export function collectDeskCandidates(root?: ParentNode | null): DeskCandidate[] {
  const scope = root ?? (typeof document === "undefined" ? null : document);
  if (!scope || typeof scope.querySelectorAll !== "function") return [];
  const nodes = scope.querySelectorAll("[data-testid]");
  const out: DeskCandidate[] = [];
  const seen = new Set<string>();
  for (const node of nodes) {
    const testid = node.getAttribute("data-testid")?.trim();
    if (!testid) continue;
    const attrs: Record<string, string> = {};
    for (const name of IDENTITY_ATTRS) {
      const value = node.getAttribute(`data-${name}`);
      if (value !== null && value !== "") attrs[name] = value;
    }
    const key = `${testid}\0${JSON.stringify(attrs)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(Object.keys(attrs).length ? { testid, attrs } : { testid });
    if (out.length >= MAX_CANDIDATES) break;
  }
  return out;
}

/**
 * If the sentence looks deictic, capture a snap and return it with the
 * original utterance. Capture failure leaves snap null. The words are never
 * rewritten and never claim a screenshot.
 */
export async function enrichWithDeskContext(
  utterance: string
): Promise<DeskEnrichment> {
  const text = utterance.trim();
  if (!text || !needsDeskContext(text)) {
    return { utterance, snap: null };
  }

  const snap = await captureDeskSnapshot();
  if (!snapshotIsUsable(snap)) return { utterance, snap: null };
  return { utterance, snap };
}

/** Older name used by the ada-path wiring. Same contract. */
export const prepareDeskContext = enrichWithDeskContext;
