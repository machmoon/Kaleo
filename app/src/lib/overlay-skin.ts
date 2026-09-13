/**
 * Which overlay the user gets. One catalogue, one stored choice.
 *
 * The brief was "have options" — the overlay is not meant to carry the
 * desktop's visual load ("for intensive stuff ofc u need the desktop but the
 * overlay isnt meant to have so much visual clutter"), and different work
 * wants different amounts of it. So the skin is a preference, not a redesign,
 * and the four here are the four that survived review rather than every idea
 * that was drawn.
 *
 * This module is deliberately pure and DOM-free: the catalogue, the stored
 * choice, and the migration for an unknown value. It is what the settings
 * control and the overlay both read, so neither owns the list.
 */

import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { safeLocalStorage } from "@/lib/storage/helper";

export type SkinId = "plain" | "spotlight" | "orb" | "terminal";

export interface SkinInfo {
  id: SkinId;
  name: string;
  /** One line, in the settings row. Says what it is, not how it looks. */
  summary: string;
  /** Where the shape came from, so the choice is traceable to real work. */
  source: string;
  /**
   * Whether the overlay actually renders this one yet.
   *
   * The catalogue is honest about this rather than offering four options of
   * which two do nothing. An unbuilt skin still stores — the preference is
   * real and survives to the build that implements it — but the picker says
   * so and the overlay falls back to the bar.
   */
  built: boolean;
}

export const SKINS: readonly SkinInfo[] = [
  {
    id: "plain",
    name: "Bar",
    summary: "The original strip — a field, a mic, a run control. Nothing else.",
    source: "Pluely's own bar, which this app is forked from",
    built: true,
  },
  {
    id: "spotlight",
    name: "Spotlight",
    summary: "A single search field that grows a result list only when there is one.",
    source: "macOS Spotlight and the cmdk pattern",
    built: true,
  },
  {
    id: "orb",
    name: "Orb",
    summary: "Collapses to a listening orb; the field appears when you speak or click.",
    source: "the iOS assistant orb",
    built: true,
  },
  {
    id: "terminal",
    name: "Terminal",
    summary: "A real shell you can also ask questions in. Capital letter asks Hardy.",
    source: "a real pty (portable-pty), with Butterfish's sigil routing",
    built: true,
  },
] as const;

export const DEFAULT_SKIN: SkinId = "plain";

const IDS = new Set<string>(SKINS.map((skin) => skin.id));

export function isSkinId(value: unknown): value is SkinId {
  return typeof value === "string" && IDS.has(value);
}

/**
 * The stored skin, or the default.
 *
 * An unrecognised value falls back rather than throwing: a downgrade after
 * trying a skin that a later build removed must not leave someone with a
 * blank overlay and no way to reach settings.
 */
export function getSkin(): SkinId {
  const stored = safeLocalStorage.getItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN);
  return isSkinId(stored) ? stored : DEFAULT_SKIN;
}

export function setSkin(id: SkinId): void {
  safeLocalStorage.setItem(KALEO_STORAGE_KEYS.OVERLAY_SKIN, id);
}

export function skinInfo(id: SkinId): SkinInfo {
  return SKINS.find((skin) => skin.id === id) ?? SKINS[0];
}

/**
 * Whether the terminal skin routes to Ada at all.
 *
 * Default **on**, because a terminal skin whose whole point is that it
 * doubles as Ada would be a plain terminal without it. Off makes it exactly
 * that — an ordinary shell — which is the escape hatch for anyone who finds
 * the capital-letter rule surprising.
 */
export function getTerminalAda(): boolean {
  return safeLocalStorage.getItem(KALEO_STORAGE_KEYS.TERMINAL_ADA) !== "0";
}

export function setTerminalAda(enabled: boolean): void {
  safeLocalStorage.setItem(KALEO_STORAGE_KEYS.TERMINAL_ADA, enabled ? "1" : "0");
}
