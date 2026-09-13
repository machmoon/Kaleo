// One finished run, handed across webviews.
//
// The overlay and the dashboard are separate webviews with separate
// `RunProvider`s (routes/index.tsx documents the gap), so the overlay's live
// run never appears in the dashboard's React tree. But the two windows already
// share same-origin localStorage — the engine address crosses windows that way
// — and the `storage` event fires in every window EXCEPT the writer, which is
// exactly the direction a hand-off wants: the overlay finishes a run, every
// other window hears about it, and the writer cannot be re-entered by its own
// announcement.
//
// Frames are deliberately left out of the payload: they are bounded but bulky,
// and everything the dashboard renders comes from `result` and `progress`. A
// payload that will not fit (storage quota) is dropped whole with a warning
// rather than truncated — a half-written run reading as a whole one is the lie
// this app refuses everywhere else.

import { safeLocalStorage } from "@/lib/storage/helper";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import type { RunHistoryEntry } from "@/hooks/useSilkscreenRun";

/** What actually crosses the window boundary: an entry minus its frame log. */
export type PublishedRun = Omit<RunHistoryEntry, "frames">;

/**
 * The most JSON this will put through storage, in UTF-16 characters.
 *
 * A browser origin gets roughly 5 MB of localStorage, shared with everything
 * else this app keeps there (engine address, overlay dock, step mode). A board
 * is a few hundred kilobytes of text, so this cap is never reached by a real
 * run; it exists because `safeLocalStorage.setItem` SWALLOWS the quota
 * exception, which would otherwise turn "too big to hand over" into "handed
 * over" with no warning and no data. Measuring first is the only way this
 * module can keep the promise in its header comment: an oversized payload is
 * dropped whole, out loud, rather than truncated into a half-run.
 */
export const MAX_PUBLISHED_CHARS = 2_000_000;

/** Announce a finished run to the other windows. Never throws. */
export function publishRun(entry: RunHistoryEntry): void {
  try {
    const { frames: _frames, ...payload } = entry;
    const json = JSON.stringify(payload satisfies PublishedRun);
    if (json.length > MAX_PUBLISHED_CHARS) {
      console.warn(
        `[kaleo bridge] run ${entry.id} is ${json.length} characters, over the ` +
          `${MAX_PUBLISHED_CHARS} the hand-off carries; not published.`
      );
      return;
    }
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.LAST_RUN, json);
  } catch (error) {
    // The hand-off is a courtesy on top of a run that already succeeded in
    // this window; failing to announce it must not fail the run.
    console.warn("[kaleo bridge] could not publish the finished run:", error);
  }
}

/**
 * The most recently published run, or null when there is none or the stored
 * value is not one this build can trust. Storage is never trusted blindly:
 * an older build (or a hand edit) must produce null, not a half-shaped entry.
 */
export function readPublishedRun(): RunHistoryEntry | null {
  const raw = safeLocalStorage.getItem(KALEO_STORAGE_KEYS.LAST_RUN);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<PublishedRun> | null;
    if (
      !parsed ||
      typeof parsed.id !== "string" ||
      typeof parsed.at !== "number" ||
      typeof parsed.intent !== "string" ||
      !parsed.request ||
      !parsed.result ||
      !parsed.progress
    ) {
      return null;
    }
    return { ...(parsed as PublishedRun), frames: [] };
  } catch {
    return null;
  }
}

/**
 * Hear runs finished in OTHER windows. Returns the unsubscribe function.
 * The writer never receives its own event — that is the `storage` event's
 * contract, not a guard this module has to maintain.
 */
export function subscribePublishedRun(
  onRun: (entry: RunHistoryEntry) => void
): () => void {
  const handler = (event: StorageEvent) => {
    if (event.key !== KALEO_STORAGE_KEYS.LAST_RUN) return;
    const entry = readPublishedRun();
    if (entry) onRun(entry);
  };
  window.addEventListener("storage", handler);
  return () => window.removeEventListener("storage", handler);
}
