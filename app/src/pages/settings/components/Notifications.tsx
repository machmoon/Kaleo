import { useEffect, useState } from "react";
import { Button, Header, Label, Switch } from "@/components";
import { getSetting, setSetting, subscribe } from "@/lib/settings/store";
import { sendTestNotification } from "@/lib/notify/notify";

export type NotifyOs = "always" | "not_focused" | "never";

const OS_OPTIONS: readonly { id: NotifyOs; label: string }[] = [
  { id: "always", label: "Always" },
  { id: "not_focused", label: "Only when you're in another app" },
  { id: "never", label: "Never" },
];

/** "Sent", never "Delivered": the plugin gives no delivery signal. */
export const SENT_LINE = "Sent, look at the top right of your screen.";
export const DEV_BANNER_LINE = "In this development build the banner says Terminal.";

interface NotificationsProps {
  /** `setup` drops the Header and the `*-settings` wrapper id. */
  variant?: "setup" | "settings";
  className?: string;
}

function readOs(): NotifyOs {
  const value = getSetting("notify.os");
  return value === "always" || value === "never" ? value : "not_focused";
}

/**
 * Native macOS banners for run milestones (contract C4).
 *
 * The test button is the opt-in: pressing it turns notifications on first,
 * and the helper text says so, because macOS lists Kaleo in System Settings
 * only after the first banner — a switch alone would leave nothing to find
 * there. Default off.
 */
export const Notifications = ({ variant = "settings", className }: NotificationsProps) => {
  const [enabled, setEnabled] = useState<boolean>(() => getSetting("notify.enabled") === true);
  const [os, setOs] = useState<NotifyOs>(readOs);
  const [result, setResult] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const offEnabled = subscribe("notify.enabled", (value) => setEnabled(value === true));
    const offOs = subscribe("notify.os", () => setOs(readOs()));
    return () => {
      offEnabled();
      offOs();
    };
  }, []);

  const toggle = (next: boolean) => {
    setEnabled(next);
    void setSetting("notify.enabled", next);
  };

  const chooseOs = (next: NotifyOs) => {
    setOs(next);
    void setSetting("notify.os", next);
  };

  const test = async () => {
    setBusy(true);
    setResult("");
    try {
      // The send itself turns notifications on (the helper says so); the
      // local switch follows so the radios unlock without a round trip.
      if (!enabled) toggle(true);
      const outcome = await sendTestNotification();
      if (outcome.sent) {
        setResult(import.meta.env.DEV ? `${SENT_LINE} ${DEV_BANNER_LINE}` : SENT_LINE);
      } else {
        setResult(`Not sent. ${outcome.reason}`);
      }
    } catch (error) {
      setResult(`Could not send: ${(error as Error)?.message ?? "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const body = (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <Label className="text-sm font-medium">Notify when a run finishes</Label>
          <p className="mt-1 text-xs text-muted-foreground">
            {enabled ? "Placement, routing, review and the case, as banners." : "Off. Nothing is shown outside the strip."}
          </p>
        </div>
        <Switch
          checked={enabled}
          onCheckedChange={toggle}
          data-testid="notify-switch"
          aria-label={enabled ? "Turn notifications off" : "Turn notifications on"}
        />
      </div>

      <div role="radiogroup" aria-label="When to show banners" className="space-y-1" data-testid="notify-os">
        {OS_OPTIONS.map((option) => (
          <label
            key={option.id}
            className={`flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-sm ${enabled ? "" : "opacity-60"}`}
          >
            <input
              type="radio"
              name="notify-os"
              value={option.id}
              checked={os === option.id}
              disabled={!enabled}
              onChange={() => chooseOs(option.id)}
              data-testid="notify-os-option"
              data-os={option.id}
              className="accent-foreground"
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" onClick={() => void test()} disabled={busy} data-testid="notify-test">
          {busy ? "Sending…" : "Send a test notification"}
        </Button>
        {!enabled ? (
          <span className="text-xs text-muted-foreground">Sending a test turns notifications on.</span>
        ) : null}
      </div>
      {result ? (
        <p className="text-xs" role="status" data-testid="notify-result">
          {result}
        </p>
      ) : null}
    </div>
  );

  if (variant === "setup") return <div className={className}>{body}</div>;

  return (
    <div id="notifications" className={`space-y-3 ${className ?? ""}`} data-testid="notifications-settings">
      <Header
        isMainTitle
        title="Notifications"
        description="Hardy tells you when a run finishes. macOS lists Hardy in System Settings only after the first banner."
      />
      {body}
    </div>
  );
};
