/**
 * Whether a deictic wake may still reach wakeAction after /desk/resolve.
 *
 * A start would send the pointing phrase (or, historically, a fake
 * "[desk: screenshot captured]" prefix) into /generate. Commands against
 * an open step run are still allowed — "change this" while a stage waits
 * is a reply, not a new board.
 */

import type { WakeAction } from "./wake-flow";

export function proceedAfterDesk(action: WakeAction): boolean {
  return action.kind !== "start";
}
