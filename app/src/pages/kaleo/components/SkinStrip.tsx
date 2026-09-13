import { Button } from "@/components";

/**
 * The way back out of a skin that replaced the bar.
 *
 * Every skin except the plain bar takes over the whole overlay, and the bar
 * is where the settings button lives — so without this, choosing Spotlight,
 * Orb or Terminal makes settings unreachable from the overlay and the only
 * escape is the dashboard window. That is a trap, not a preference.
 *
 * It also carries `data-tauri-drag-region`, which the bar normally provides.
 * A skin without it is an always-on-top window the user cannot move off
 * whatever they need to see underneath.
 */
export const SkinStrip = ({
  name,
  onLeave,
}: {
  name: string;
  onLeave: () => void;
}) => (
  <div
    data-tauri-drag-region
    className="flex w-full shrink-0 items-center justify-between gap-2 border-b border-input/40 px-2 py-1"
  >
    <span className="text-[11px] text-muted-foreground">{name}</span>
    <Button
      size="sm"
      variant="ghost"
      className="h-6 px-2 text-[11px]"
      onClick={onLeave}
      data-testid="skin-leave"
      title="Back to the bar"
      aria-label="Back to the bar"
    >
      Back to the bar
    </Button>
  </div>
);
