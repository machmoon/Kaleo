import { useEffect, useState } from "react";
import { Button, Label } from "@/components/ui";
import { StepFrame } from "@/components/setup";
import { Notifications } from "@/pages/settings/components/Notifications";
import { WakeWordToggle } from "@/pages/settings/components/WakeWordToggle";
import { useApp } from "@/contexts";
import { getSetting, subscribe } from "@/lib/settings/store";
import { ensureMicrophoneAccess, type CaptureAccess } from "@/lib/capture-permission";
import type { SetupCardId } from "@/lib/setup/machine";

export const PERMISSIONS_TITLE = "Permissions";
export const PERMISSIONS_SUBTITLE = "Hardy tells you when a run finishes, and listens only when you ask.";

/** macOS' own deep link to the microphone pane. */
export const MIC_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";

interface PermissionsStepProps {
  setCard: (card: SetupCardId, done: boolean) => void;
  /** Injected so tests need no plugin; defaults to the real prompt. */
  requestMic?: () => Promise<CaptureAccess>;
  openSettings?: (url: string) => Promise<void>;
}

async function openSettingsUrl(url: string): Promise<void> {
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
}

export const PermissionsStep = ({
  setCard,
  requestMic = ensureMicrophoneAccess,
  openSettings = openSettingsUrl,
}: PermissionsStepProps) => {
  const { customizable } = useApp();
  const [mic, setMic] = useState<CaptureAccess | "unasked" | "asking">("unasked");
  const [micWhy, setMicWhy] = useState("");

  useEffect(() => {
    setCard("notifications", getSetting("notify.enabled") === true);
    return subscribe("notify.enabled", (value) => setCard("notifications", value === true));
  }, [setCard]);

  const wake = customizable.wakeWord.isEnabled;
  useEffect(() => {
    setCard("voice", wake);
  }, [wake, setCard]);

  const askMic = async () => {
    setMic("asking");
    setMicWhy("");
    try {
      setMic(await requestMic());
    } catch (error) {
      const message = (error as Error)?.message ?? "";
      setMic(/no microphone access|could not use/i.test(message) ? "unavailable" : "denied");
      setMicWhy(message);
    }
  };

  return (
    <StepFrame stepId="permissions" title={PERMISSIONS_TITLE} subtitle={PERMISSIONS_SUBTITLE}>
      <div className="space-y-4 text-left">
        <div className="rounded-lg border bg-card p-3" data-testid="permissions-notifications">
          <Notifications variant="setup" />
        </div>

        <div className="space-y-2 rounded-lg border bg-card p-3" data-testid="permissions-mic" data-state={mic}>
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <Label className="text-sm font-medium">Microphone</Label>
              <p className="mt-1 text-xs text-muted-foreground">
                {mic === "granted"
                  ? "Allowed. The mic button in the strip dictates a board."
                  : mic === "denied"
                    ? "Refused. Turn it on for Hardy in System Settings → Privacy & Security → Microphone."
                    : mic === "unavailable"
                      ? "No microphone is available here."
                      : "The mic button always works once this is allowed. macOS asks once."}
              </p>
              {micWhy && mic !== "granted" ? (
                <p className="mt-1 text-[11px] text-muted-foreground">{micWhy}</p>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {mic === "denied" ? (
                <Button size="sm" variant="ghost" onClick={() => void openSettings(MIC_SETTINGS_URL)} data-testid="mic-open-settings">
                  Open System Settings
                </Button>
              ) : null}
              {mic !== "granted" ? (
                <Button size="sm" variant="outline" onClick={() => void askMic()} disabled={mic === "asking"} data-testid="mic-allow">
                  {mic === "asking" ? "Asking…" : mic === "unasked" ? "Allow microphone" : "Try again"}
                </Button>
              ) : null}
            </div>
          </div>
        </div>

        <div className="rounded-lg border bg-card p-3" data-testid="permissions-wake">
          <p className="mb-2 text-sm font-medium">Hey Ada</p>
          <p className="mb-2 text-xs text-muted-foreground">
            Wake-word listening stays off unless you turn it on. The mic button always works.
          </p>
          <WakeWordToggle variant="setup" />
        </div>
      </div>
    </StepFrame>
  );
};
