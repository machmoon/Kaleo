import { useState } from "react";
import { Header, Label, Switch } from "@/components";
import { SkinPreviewPicker } from "@/components/setup";
import { type SkinId, getSkin, getTerminalAda, setTerminalAda } from "@/lib/overlay-skin";

/**
 * Pick the overlay's shape.
 *
 * The brief was "have options": the overlay carries a control strip over
 * KiCad, and how much of one it should be depends on the work. The catalogue
 * lives in `@/lib/overlay-skin` so this control, the wizard and the overlay
 * read the same list; `SkinPreviewPicker` draws it here and in the wizard.
 *
 * The choice is stored, not lifted into a context, because the overlay is a
 * separate webview from this one. localStorage plus a `storage` event is how
 * the two windows already talk (see `KALEO_STORAGE_KEYS.LAST_RUN`), so a skin
 * picked here reaches the overlay without a new bridge.
 */
export const OverlaySkin = ({ className }: { className?: string }) => {
  const [skin, setSkinState] = useState<SkinId>(() => getSkin());
  const [adaInTerminal, setAdaInTerminal] = useState(() => getTerminalAda());

  const toggleAda = (enabled: boolean) => {
    setTerminalAda(enabled);
    setAdaInTerminal(enabled);
  };

  return (
    <div id="overlay-skin" className={`space-y-3 ${className ?? ""}`} data-testid="overlay-skin-settings">
      <Header
        isMainTitle
        title="Overlay"
        description="How much the strip shows. The desktop is for intensive work; this is meant to stay out of the way."
      />
      <SkinPreviewPicker onChange={setSkinState} />

      {/* Only meaningful for the one skin that has a shell in it. */}
      {skin === "terminal" ? (
        <div className="flex items-center justify-between gap-3 rounded-md border px-3 py-2">
          <div className="min-w-0">
            <Label className="text-sm font-medium">Ask Hardy from the terminal</Label>
            <p className="mt-1 text-xs text-muted-foreground">
              A line starting with a Capital letter goes to Hardy; <code>!</code> asks her to
              work on it. A leading space always runs in the shell. Off makes this an
              ordinary terminal.
            </p>
          </div>
          <Switch
            checked={adaInTerminal}
            onCheckedChange={toggleAda}
            data-testid="terminal-ada-switch"
            aria-label={adaInTerminal ? "Turn off Hardy routing" : "Turn on Hardy routing"}
          />
        </div>
      ) : null}
    </div>
  );
};
