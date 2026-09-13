import { Switch, Label, Header } from "@/components";
import { useApp } from "@/contexts";
import { WAKE_WORD } from "@/lib/wake-word";

interface WakeWordToggleProps {
  className?: string;
  /** `setup` drops the Header and the `*-settings` wrapper id. */
  variant?: "setup" | "settings";
}

/**
 * The Settings face of the ear. Same persisted switch the overlay flips —
 * off by default. On Mac this is one Gemini clip per click, not always-on
 * like Siri; the mic button is the reliable voice path.
 */
export const WakeWordToggle = ({ className, variant = "settings" }: WakeWordToggleProps) => {
  const { customizable, toggleWakeWord } = useApp();
  const on = customizable.wakeWord.isEnabled;

  const row = (
    <div className="flex items-center justify-between">
      <div className="flex items-center space-x-3">
        <div>
          <Label className="text-sm font-medium">
            {on ? "Listening once (not always-on)" : "Wake spotting off"}
          </Label>
          <p className="text-xs text-muted-foreground mt-1">
            {on
              ? `Speak what you need, or “${WAKE_WORD}, …”. This listen ends after one clip.`
              : "Use the mic button to dictate. Turn this on only for one experimental listen."}
          </p>
        </div>
      </div>
      <Switch
        checked={on}
        onCheckedChange={toggleWakeWord}
        data-testid="wake-word-switch"
        title={on ? "Turn off one-shot listen" : "Arm one experimental listen"}
        aria-label={on ? "Turn off one-shot listen" : "Arm one experimental listen"}
      />
    </div>
  );

  if (variant === "setup") return <div className={className}>{row}</div>;

  return (
    <div id="wake-word" className={`space-y-2 ${className ?? ""}`} data-testid="wake-word-settings">
      <Header
        title={`“Hey ${WAKE_WORD}” (experimental)`}
        description={`One-shot listen — not always-on like Siri. On Mac each click sends one short Gemini transcript, then the ear turns off. The mic button is the reliable way to dictate.`}
        isMainTitle
      />
      {row}
    </div>
  );
};
