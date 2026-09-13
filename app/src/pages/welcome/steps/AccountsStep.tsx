import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { Button, Input, Label } from "@/components/ui";
import { ConnectCard, StepFrame, type ConnectCardState } from "@/components/setup";
import { BillingSetup } from "@/pages/settings/components/BillingSetup";
import type { SetupCardId } from "@/lib/setup/machine";
import {
  ENGINE_DOWN_SENTENCE,
  consentUrlAllowed,
  disconnect,
  googleConnect,
  googlePoll,
  googleSaveCredentials,
  microsoftSave,
  stillConfiguredSentence,
  environmentWinsSentence,
  type SetupItemState,
  type SetupReport,
} from "@/lib/setup/service";
import type { SetupReportState } from "../useSetupReport";

export const ACCOUNTS_TITLE = "Connect accounts";
export const ACCOUNTS_SUBTITLE = "Calendar, Gmail and Chat for delivering work; Stripe for orders. Each one is optional.";

export const GOOGLE_POLL_MS = 2000;
export const SIGNED_OUT_SENTENCE =
  "Signed out here: the refresh token was deleted from this Mac, not revoked at Google. Revoke it at myaccount.google.com/permissions.";
export const GOOGLE_DEMO_SENTENCE = "Demo sign-in recorded. No Google account was contacted.";
export const MICROSOFT_DEMO_SENTENCE = "Demo: ids accepted by shape. Entra was not contacted.";
export const CONSENT_HOST_REFUSED = "the engine returned a consent URL on an unexpected host";

function cardState(state: SetupItemState | undefined, down: boolean): ConnectCardState {
  if (down) return "down";
  switch (state) {
    case "ready":
      return "ready";
    case "partial":
      return "partial";
    case "unavailable":
      return "unavailable";
    default:
      return "unconfigured";
  }
}

async function openInBrowser(url: string): Promise<void> {
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
}

interface CardProps {
  baseUrl: string;
  token: string;
  report: SetupReport | null;
  down: boolean;
  refresh: () => Promise<SetupReport | null>;
}

const GoogleCard = ({ baseUrl, token, report, down, refresh }: CardProps) => {
  const google = report?.google;
  const demo = google?.demo === true;
  const [waiting, setWaiting] = useState(false);
  const [note, setNote] = useState("");
  const [showClient, setShowClient] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const timer = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
    setWaiting(false);
  }, []);
  useEffect(() => stopPolling, [stopPolling]);

  const poll = useCallback(() => {
    stopPolling();
    setWaiting(true);
    timer.current = window.setInterval(() => {
      void googlePoll(baseUrl, token)
        .then(async (status) => {
          if (status.job.state === "waiting") return;
          stopPolling();
          if (status.job.state === "failed") setNote(status.job.error || "The sign-in did not finish.");
          await refresh();
        })
        .catch((error: Error) => {
          stopPolling();
          setNote(error.message);
        });
    }, GOOGLE_POLL_MS);
  }, [baseUrl, token, refresh, stopPolling]);

  const connect = async () => {
    setNote("");
    try {
      const started = await googleConnect(baseUrl, token);
      const mode = started.demo ? "demo" : (report?.mode ?? "live");
      const allowed = consentUrlAllowed(started.auth_url, baseUrl, mode);
      if (!allowed.ok) {
        setNote(allowed.reason);
        return;
      }
      await openInBrowser(started.auth_url);
      poll();
    } catch (error) {
      setNote((error as Error).message);
    }
  };

  const signOut = async () => {
    setNote("");
    try {
      const response = await disconnect(baseUrl, token, "google");
      setNote([SIGNED_OUT_SENTENCE, stillConfiguredSentence(response)].filter(Boolean).join(" "));
      await refresh();
    } catch (error) {
      setNote((error as Error).message);
    }
  };

  const saveClient = async () => {
    setNote("");
    try {
      const response = await googleSaveCredentials(baseUrl, token, {
        client_id: clientId.trim(),
        client_secret: clientSecret,
      });
      setNote(response.note || "OAuth client saved; proven at sign-in.");
      setClientSecret("");
      setShowClient(false);
      await refresh();
    } catch (error) {
      setNote((error as Error).message);
    }
  };

  const state: ConnectCardState = waiting || google?.job.state === "waiting" ? "connecting" : cardState(google?.state, down);
  const needsClient = !down && !demo && google !== undefined && !google.oauth_client;
  const detail = down
    ? ENGINE_DOWN_SENTENCE
    : demo && state === "ready"
      ? GOOGLE_DEMO_SENTENCE
      : google?.detail || "";

  return (
    <ConnectCard
      id="google"
      name="Google"
      description="Calendar, Gmail and Chat for delivering work."
      state={state}
      demo={demo}
      detail={detail}
      actionLabel={needsClient ? "Add OAuth client" : "Connect"}
      onAction={needsClient ? () => setShowClient((v) => !v) : () => void connect()}
      secondaryLabel="Disconnect"
      onSecondary={() => void signOut()}
      readyWord={google?.signed_in ? "Signed in" : "Connected"}
    >
      {showClient && needsClient ? (
        <div className="space-y-2" data-testid="google-client-fields">
          <Input
            placeholder="Client id (…apps.googleusercontent.com)"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            data-testid="google-client-id"
          />
          <Input
            type="password"
            autoComplete="off"
            placeholder="Client secret"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            data-testid="google-client-secret"
          />
          <Button
            size="sm"
            variant="outline"
            disabled={!clientId.trim().endsWith(".apps.googleusercontent.com") || !clientSecret}
            onClick={() => void saveClient()}
            data-testid="google-client-save"
          >
            Save client
          </Button>
        </div>
      ) : null}
      {note ? (
        <p className="text-xs text-muted-foreground" data-testid="google-note">
          {note}
        </p>
      ) : null}
    </ConnectCard>
  );
};

const MicrosoftCard = ({ baseUrl, token, report, down, refresh }: CardProps) => {
  const microsoft = report?.microsoft;
  const demo = microsoft?.demo === true;
  const [appId, setAppId] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);

  const save = async () => {
    setBusy(true);
    setNote("");
    try {
      const response = await microsoftSave(baseUrl, token, {
        app_id: appId.trim(),
        app_secret: appSecret,
        tenant_id: tenantId.trim(),
      });
      if (response.saved) {
        setNote([response.note ?? "", environmentWinsSentence(response)].filter(Boolean).join(" "));
        setAppSecret("");
        setEditing(false);
        await refresh();
      } else {
        setNote(response.verdict?.meaning || "Entra refused the registration.");
      }
    } catch (error) {
      setNote((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setNote("");
    try {
      const response = await disconnect(baseUrl, token, "microsoft");
      setNote(stillConfiguredSentence(response) || "Registration removed from this Mac.");
      await refresh();
    } catch (error) {
      setNote((error as Error).message);
    }
  };

  const state: ConnectCardState = busy ? "connecting" : cardState(microsoft?.state, down);
  const showFields = !down && state !== "unavailable" && (state !== "ready" || editing);
  const detail = down ? ENGINE_DOWN_SENTENCE : demo && state === "ready" ? MICROSOFT_DEMO_SENTENCE : microsoft?.detail || "";

  return (
    <ConnectCard
      id="microsoft"
      name="Microsoft"
      description="Teams meetings and chat."
      state={state}
      demo={demo}
      detail={detail}
      readyWord="Token issued"
      secondaryLabel="Disconnect"
      onSecondary={() => void remove()}
    >
      {showFields ? (
        <div className="space-y-2" data-testid="microsoft-fields">
          <Input placeholder="Application (client) id" value={appId} onChange={(e) => setAppId(e.target.value)} data-testid="microsoft-app-id" />
          <Input
            type="password"
            autoComplete="off"
            placeholder="Client secret"
            value={appSecret}
            onChange={(e) => setAppSecret(e.target.value)}
            data-testid="microsoft-app-secret"
          />
          <Input placeholder="Directory (tenant) id" value={tenantId} onChange={(e) => setTenantId(e.target.value)} data-testid="microsoft-tenant-id" />
          <Button
            size="sm"
            variant="outline"
            disabled={busy || !appId.trim() || !appSecret || !tenantId.trim()}
            onClick={() => void save()}
            data-testid="microsoft-save"
          >
            {busy ? "Asking Entra…" : "Verify and save"}
          </Button>
        </div>
      ) : state === "ready" ? (
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)} data-testid="microsoft-edit">
          Replace registration
        </Button>
      ) : null}
      {note ? (
        <p className="text-xs text-muted-foreground" data-testid="microsoft-note">
          {note}
        </p>
      ) : null}
      {microsoft?.meaning ? (
        <p className="text-[11px] leading-snug text-muted-foreground" data-testid="microsoft-meaning">
          {microsoft.meaning}
        </p>
      ) : null}
    </ConnectCard>
  );
};

const StripeCard = ({ baseUrl, token, report, down, refresh }: CardProps) => {
  const stripe = report?.stripe;
  const demo = stripe?.demo === true;
  const [editing, setEditing] = useState(false);
  const state = cardState(stripe?.state, down);
  const showFields = !down && state !== "unavailable" && (state !== "ready" || editing);
  return (
    <ConnectCard
      id="stripe"
      name="Billing"
      description="Stripe handles fabrication and part orders. Demo mode makes no charges."
      state={state}
      demo={demo}
      detail={down ? ENGINE_DOWN_SENTENCE : stripe?.detail || ""}
      readyWord={demo ? "Demo key stored" : "Key verified"}
      secondaryLabel="Change"
      onSecondary={() => setEditing((v) => !v)}
    >
      {showFields ? (
        <BillingSetup
          variant="setup"
          baseUrl={baseUrl}
          token={token}
          saveUrl="/setup/stripe/credentials"
          mode={report?.mode ?? "live"}
          onSaved={() => {
            setEditing(false);
            void refresh();
          }}
        />
      ) : null}
    </ConnectCard>
  );
};

interface AccountsStepProps {
  baseUrl: string;
  token: string;
  report: SetupReportState;
  setCard: (card: SetupCardId, done: boolean) => void;
}

export const AccountsStep = ({ baseUrl, token, report, setCard }: AccountsStepProps) => {
  const [more, setMore] = useState(false);
  const data = report.report;

  // A card counts as done when the engine says ready; the skipped line on
  // the done screen is built from what the engine said, not what was pressed.
  useEffect(() => {
    if (!data) return;
    setCard("google", data.google.state === "ready");
    setCard("stripe", data.stripe.state === "ready");
    setCard("microsoft", data.microsoft.state === "ready");
  }, [data, setCard]);

  const shared: CardProps = { baseUrl, token, report: data, down: report.down, refresh: report.refresh };

  return (
    <StepFrame stepId="accounts" title={ACCOUNTS_TITLE} subtitle={ACCOUNTS_SUBTITLE}>
      <div className="space-y-3">
        {report.error ? (
          <p className="text-xs text-destructive" data-testid="accounts-error">
            {report.error}
          </p>
        ) : null}
        <GoogleCard {...shared} />
        <StripeCard {...shared} />
        <div>
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            onClick={() => setMore((v) => !v)}
            aria-expanded={more}
            data-testid="accounts-more"
          >
            {more ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
            <Label className="cursor-pointer text-xs font-normal">More accounts</Label>
          </button>
          {more ? (
            <div className="mt-2">
              <MicrosoftCard {...shared} />
            </div>
          ) : null}
        </div>
      </div>
    </StepFrame>
  );
};
