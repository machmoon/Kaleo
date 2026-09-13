import { Badge } from "@/components/ui";
import { DEMO_BANNER } from "@/lib/setup/service";

/** The word on a card whose connection was a demo one. */
export const DemoBadge = () => (
  <Badge variant="outline" className="text-[11px] text-muted-foreground" data-testid="demo-badge">
    Demo
  </Badge>
);

/**
 * The sentence on every wizard step while the engine runs in demo mode.
 *
 * Persistent on purpose: a demo sign-in looks exactly like a real one, so
 * the one line that says nothing reached Google, Microsoft or Stripe cannot
 * be a toast that scrolls away.
 */
export const DemoBanner = () => (
  <p
    role="note"
    className="rounded-md border border-dashed px-3 py-2 text-center text-xs text-muted-foreground"
    data-testid="demo-banner"
  >
    {DEMO_BANNER}
  </p>
);
