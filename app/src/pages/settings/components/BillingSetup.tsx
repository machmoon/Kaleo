import { useCallback, useEffect, useState } from "react";
import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { Button, Header, Input, Label } from "@/components";
import { DEFAULT_BASE_URL, authHeaders } from "@/lib/silkscreen/client";
import { environmentWinsSentence } from "@/lib/setup/service";

/**
 * The Settings face of billing setup — and, as `variant="setup"`, the
 * wizard's Stripe card.
 *
 * The ask was "why can't it just be a popup where I click allow". It can —
 * an earlier version of this file asserted otherwise and was wrong. Stripe
 * runs an OAuth authorization server at access.stripe.com/mcp with dynamic
 * client registration, PKCE S256 and no client secret, which is the same
 * shape as this app's Google sign-in. "Connect Stripe" below opens it.
 *
 * What the popup does not do is hand back an API key: it grants the `mcp`
 * scope, so it proves the account is yours but the REST calls this app makes
 * still run on a restricted key. Both halves are on screen rather than one
 * being quietly implied — the pane says what consent covers and what it does
 * not, the zero-friction CLI commands stay copyable, and a key is verified
 * against Stripe live before it is ever saved, because saving an unverified
 * key is how you believe billing works until a customer tries to pay.
 *
 * Every call goes through Tauri's fetch with the bearer, like the rest of
 * the app. In demo mode the OAuth button is hidden — `/billing/connect`
 * reaches access.stripe.com, and demo makes no outbound call — and a "Use
 * the demo key" button fills the one literal the demo engine accepts.
 */

interface Step {
    key: string;
    what: string;
    fix: string;
    present: boolean;
    preview: string | null;
    warning: string | null;
}

interface Report {
    ready: boolean;
    mode: string;
    steps: Step[];
    quickstart: { label: string; command: string; note: string }[];
    oauth_available: boolean;
    oauth_note: string;
    storage?: { path: string; note: string };
}

interface Connection {
    connected: boolean;
    status: string;
    covers: string;
    does_not_cover: string;
    flow: {
        state: "idle" | "running" | "connected" | "failed";
        url: string | null;
        error: string | null;
    };
}

const FIELDS: { key: string; label: string; placeholder: string }[] = [
    {
        key: "STRIPE_API_KEY",
        label: "Restricted key",
        placeholder: "rk_test_…",
    },
    {
        key: "STRIPE_WEBHOOK_SECRET",
        label: "Webhook signing secret",
        placeholder: "whsec_…",
    },
    {
        key: "STRIPE_PRICE_ID",
        label: "Credit pack price",
        placeholder: "price_…",
    },
];

/** The one key the demo engine stores. Anything real-looking is refused. */
export const DEMO_STRIPE_KEY = "rk_test_kaleo_demo";

/** What a verified live key proves, and what it does not. */
export const LIVE_VERIFIED_LINE =
    "Key verified with Stripe. Webhook secret and price are not proven until a payment.";

const TIMEOUT_MS = 15_000;

export interface BillingSetupProps {
    baseUrl?: string;
    token?: string;
    /** `setup` drops the Header, the wrapper id and the storage footnote. */
    variant?: "setup" | "settings";
    /** Where "Verify and save" posts. The wizard uses `/setup/stripe/credentials`. */
    saveUrl?: string;
    /** The engine's `/setup` mode. Demo hides the OAuth button and offers the demo key. */
    mode?: "live" | "demo";
    /** After a successful save (not a verify-only check). */
    onSaved?: () => void;
}

export const BillingSetup = ({
    baseUrl = DEFAULT_BASE_URL,
    token = "",
    variant = "settings",
    saveUrl = "/billing/config",
    mode = "live",
    onSaved,
}: BillingSetupProps) => {
    const [report, setReport] = useState<Report | null>(null);
    const [values, setValues] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<string | null>(null);
    const [copied, setCopied] = useState<string | null>(null);
    const [conn, setConn] = useState<Connection | null>(null);
    const demo = mode === "demo";

    const request = useCallback(
        (route: string, method: "GET" | "POST", body?: unknown) =>
            tauriFetch(`${baseUrl}${route}`, {
                method,
                headers:
                    method === "POST"
                        ? { "Content-Type": "application/json", ...authHeaders(token) }
                        : authHeaders(token),
                ...(method === "POST" ? { body: JSON.stringify(body ?? {}) } : {}),
                signal: AbortSignal.timeout(TIMEOUT_MS),
            }),
        [baseUrl, token],
    );

    // Both readers refuse a body that is not the documented shape: a
    // settings pane must not crash on an engine that answered something else.
    const load = useCallback(async () => {
        try {
            const res = await request("/billing/config", "GET");
            const body = (await res.json()) as Partial<Report> | null;
            setReport(body && Array.isArray(body.steps) ? (body as Report) : null);
        } catch {
            setReport(null);
        }
    }, [request]);

    const loadConnection = useCallback(async (): Promise<Connection | null> => {
        // Demo makes no outbound call, so there is no OAuth flow to ask about.
        if (demo) {
            setConn(null);
            return null;
        }
        try {
            const body = (await (await request("/billing/connect", "GET")).json()) as Partial<Connection> | null;
            const next = body && typeof body.flow === "object" && body.flow !== null ? (body as Connection) : null;
            setConn(next);
            return next;
        } catch {
            setConn(null);
            return null;
        }
    }, [request, demo]);

    useEffect(() => {
        void load();
        void loadConnection();
    }, [load, loadConnection]);

    /**
     * Consent waits on a human, so the service answers 202 and we poll. The
     * interval is cleared on unmount *and* on a terminal state — a settings
     * pane the user navigated away from must not keep polling forever.
     */
    useEffect(() => {
        if (conn?.flow.state !== "running") return;
        const timer = window.setInterval(() => {
            void loadConnection().then((next) => {
                if (next && next.flow.state !== "running")
                    window.clearInterval(timer);
            });
        }, 1000);
        return () => window.clearInterval(timer);
    }, [conn?.flow.state, loadConnection]);

    const connect = async () => {
        setResult(null);
        try {
            await request("/billing/connect", "POST");
            await loadConnection();
        } catch {
            setResult("The engine is not reachable. Start it and try again.");
        }
    };

    const disconnect = async () => {
        try {
            await request("/billing/disconnect", "POST");
        } finally {
            await loadConnection();
        }
    };

    const submit = async (verifyOnly: boolean) => {
        setBusy(true);
        setResult(null);
        try {
            const res = await request(verifyOnly ? "/billing/config" : saveUrl, "POST", {
                ...values,
                verify_only: verifyOnly,
            });
            const body = await res.json();
            if (body.check?.ok) {
                const environment = environmentWinsSentence(body);
                if (verifyOnly) {
                    setResult(`Key works — ${body.check.mode} mode.`);
                } else if (demo) {
                    setResult(
                        [body.note ?? `Saved. ${body.check.mode} mode; no charge can happen in demo.`, environment]
                            .filter(Boolean)
                            .join(" "),
                    );
                } else {
                    setResult([LIVE_VERIFIED_LINE, environment].filter(Boolean).join(" "));
                }
                // The key never comes back, so clear the field once it is stored.
                if (!verifyOnly) {
                    setValues({});
                    onSaved?.();
                }
                await load();
            } else {
                setResult(
                    body.check?.reason ??
                        body.error ??
                        "Could not verify that key.",
                );
            }
        } catch {
            setResult("The engine is not reachable. Start it and try again.");
        } finally {
            setBusy(false);
        }
    };

    const copy = async (command: string) => {
        await navigator.clipboard.writeText(command);
        setCopied(command);
        window.setTimeout(() => setCopied(null), 1500);
    };

    const body = (
        <>
            {conn && !demo ? (
                <div
                    className="space-y-1 rounded-md border p-3"
                    data-testid="billing-connect"
                >
                    <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                            <Label className="text-sm font-medium">
                                Stripe account
                            </Label>
                            <p className="text-xs text-muted-foreground">
                                {conn.connected
                                    ? "Connected. Revoke any time in the Stripe Dashboard under user settings → OAuth sessions."
                                    : "Sign in with Stripe in your browser — no key to copy."}
                            </p>
                        </div>
                        <Button
                            variant={conn.connected ? "outline" : "default"}
                            size="sm"
                            disabled={conn.flow.state === "running"}
                            onClick={() =>
                                void (conn.connected ? disconnect() : connect())
                            }
                            data-testid="billing-connect-button"
                        >
                            {conn.flow.state === "running"
                                ? "Waiting for you…"
                                : conn.connected
                                  ? "Disconnect"
                                  : "Connect Stripe"}
                        </Button>
                    </div>
                    {conn.flow.state === "running" && conn.flow.url ? (
                        <p className="text-[11px] text-muted-foreground">
                            A browser tab should have opened.{" "}
                            <a
                                className="underline"
                                href={conn.flow.url}
                                target="_blank"
                                rel="noreferrer"
                            >
                                Open it yourself
                            </a>{" "}
                            if it did not.
                        </p>
                    ) : null}
                    {conn.flow.state === "failed" && conn.flow.error ? (
                        <p
                            className="text-xs text-destructive"
                            data-testid="billing-connect-error"
                        >
                            {conn.flow.error}
                        </p>
                    ) : null}
                    {/* The load-bearing sentence. Consent is real, and it is not a key. */}
                    <p className="text-[11px] text-muted-foreground">
                        {conn.does_not_cover}
                    </p>
                </div>
            ) : report && !report.oauth_available && !demo ? (
                <p className="text-xs text-muted-foreground">
                    {report.oauth_note}
                </p>
            ) : null}

            {variant === "settings" && report?.quickstart?.length ? (
                <div className="space-y-2" data-testid="billing-quickstart">
                    {report.quickstart.map((q) => (
                        <div
                            key={q.command}
                            className="flex items-center justify-between gap-3"
                        >
                            <div className="min-w-0">
                                <Label className="text-sm font-medium">
                                    {q.label}
                                </Label>
                                <code className="mt-1 block truncate text-xs text-muted-foreground">
                                    {q.command}
                                </code>
                            </div>
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() => void copy(q.command)}
                                title={q.note}
                                aria-label={`Copy: ${q.command}`}
                            >
                                {copied === q.command ? "Copied" : "Copy"}
                            </Button>
                        </div>
                    ))}
                </div>
            ) : null}

            <div className="space-y-3">
                {FIELDS.map((field) => {
                    const step = report?.steps.find((s) => s.key === field.key);
                    return (
                        <div key={field.key} className="space-y-1">
                            <div className="flex items-center justify-between">
                                <Label className="text-sm font-medium">
                                    {field.label}
                                </Label>
                                <span
                                    className="text-xs text-muted-foreground"
                                    data-testid={`billing-state-${field.key}`}
                                >
                                    {step?.present
                                        ? (step.preview ?? "set")
                                        : "not set"}
                                </span>
                            </div>
                            <Input
                                type="password"
                                autoComplete="off"
                                spellCheck={false}
                                placeholder={
                                    step?.present
                                        ? "•••• stored — paste to replace"
                                        : field.placeholder
                                }
                                value={values[field.key] ?? ""}
                                onChange={(e) =>
                                    setValues((v) => ({
                                        ...v,
                                        [field.key]: e.target.value,
                                    }))
                                }
                                data-testid={`billing-input-${field.key}`}
                            />
                            {step?.warning ? (
                                <p className="text-xs text-destructive">
                                    {step.warning}
                                </p>
                            ) : !step?.present && variant === "settings" ? (
                                <p className="text-xs text-muted-foreground">
                                    {step?.fix}
                                </p>
                            ) : null}
                        </div>
                    );
                })}
            </div>

            <div className="flex flex-wrap items-center gap-2">
                <Button
                    size="sm"
                    disabled={busy || !values.STRIPE_API_KEY}
                    onClick={() => void submit(false)}
                    data-testid="billing-save"
                >
                    {busy ? "Checking…" : "Verify and save"}
                </Button>
                <Button
                    variant="outline"
                    size="sm"
                    disabled={busy || !values.STRIPE_API_KEY}
                    onClick={() => void submit(true)}
                    data-testid="billing-verify"
                >
                    Check without saving
                </Button>
                {demo ? (
                    <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() =>
                            setValues((v) => ({ ...v, STRIPE_API_KEY: DEMO_STRIPE_KEY }))
                        }
                        data-testid="billing-demo-key"
                    >
                        Use the demo key
                    </Button>
                ) : null}
            </div>

            {result ? (
                <p className="text-xs" data-testid="billing-result">
                    {result}
                </p>
            ) : null}

            {variant === "settings" && report?.storage ? (
                <p className="text-[11px] text-muted-foreground">
                    Stored at <code>{report.storage.path}</code>, readable only
                    by you. {report.storage.note}
                </p>
            ) : null}
        </>
    );

    if (variant === "setup") {
        return <div className="space-y-3">{body}</div>;
    }

    return (
        <div id="billing" className="space-y-3" data-testid="billing-settings">
            <Header
                isMainTitle
                title="Billing"
                description={
                    report?.ready
                        ? `Configured in ${report.mode} mode. Runs draw down credits.`
                        : "Not set up yet. Runs are free and unmetered until it is."
                }
            />
            {body}
        </div>
    );
};
