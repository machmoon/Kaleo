import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2Icon, Loader2Icon, XCircleIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { EngineStartCommands, StepFrame } from "@/components/setup";
import { useEngineHealth } from "@/hooks/useEngineHealth";
import { fetchKeyStatus, keyAllowsContinue, type KeyStatus } from "@/lib/setup/engine";
import { listCliTools, type CliToolInfo } from "@/lib/cli";
import type { SetupCardId } from "@/lib/setup/machine";

export const ENGINE_TITLE = "Start the engine";
export const ENGINE_TITLE_DOWN = "Start the engine in a terminal";
export const ENGINE_SUBTITLE =
  "Two things live outside Hardy: the engine that generates boards, and KiCad, the canvas she works on.";

/** How long a healthy engine sits on screen before the wizard moves on. */
export const AUTO_ADVANCE_MS = 600;
export const ENGINE_POLL_MS = 2000;

/** Verbatim from `app/src-tauri/src/cli.rs`, which is the only reader of it. */
export const KICAD_ENV_VAR = "KICAD_CLI";
export const KICAD_MISSING_FIX =
  "Install KiCad, or set KICAD_CLI to the kicad-cli binary if it lives somewhere off PATH.";
/** What finding the binary does and does not prove. Never "KiCad works". */
export const KICAD_FOUND_CAVEAT = "Found on this Mac. Hardy did not run it, so the version is not known.";
export const KICAD_MISSING_COST =
  "Hardy still designs boards without it. What stops working is showing a stage in KiCad, the ERC and DRC checks, and the 3D export the order step ships.";
export const KICAD_UNKNOWN =
  "Hardy could not ask this machine what it has — that answer only exists in the desktop app.";

type KicadState = "checking" | "found" | "missing" | "unknown";

interface EngineStepProps {
  baseUrl: string;
  token: string;
  /** Whether Continue may be enabled, whenever the answer changes. */
  onCanContinue: (allowed: boolean) => void;
  /** Fired once, after the first healthy probe with a usable key row. */
  onAutoAdvance: () => void;
  /** Records the KiCad card, so the "Skipped: …" line can name it. */
  setCard?: (card: SetupCardId, done: boolean) => void;
  /** Injected so tests need no Tauri IPC; defaults to the real allowlist probe. */
  probeTools?: () => Promise<CliToolInfo[]>;
}

/**
 * The app cannot start Python. When nothing answers, this screen is the
 * terminal commands with Copy; when something does, it says exactly what
 * answered ("Answered /healthz at …", never "Running") and then asks the
 * second question, whether that engine has a key, in the service's words.
 */
export const EngineStep = ({
  baseUrl,
  token,
  onCanContinue,
  onAutoAdvance,
  setCard,
  probeTools = listCliTools,
}: EngineStepProps) => {
  const navigate = useNavigate();
  const health = useEngineHealth(baseUrl, ENGINE_POLL_MS, token);
  const [key, setKey] = useState<KeyStatus | null>(null);
  const [keyChecked, setKeyChecked] = useState(false);
  const [kicad, setKicad] = useState<KicadState>("checking");
  const [kicadDetail, setKicadDetail] = useState("");
  const advanced = useRef(false);

  // The canvas, asked once. `cli.rs` already resolves `kicad-cli` for the
  // Settings pane; the wizard asks the same question at the moment it matters
  // instead of leaving it in a pane a new user has no reason to open.
  useEffect(() => {
    let live = true;
    probeTools()
      .then((tools) => {
        if (!live) return;
        const tool = tools.find((candidate) => candidate.id === "kicad-cli");
        if (!tool) {
          // A build with no allowlist compiled in is a build problem, not a
          // missing install, so it must not read as "install KiCad".
          setKicad("unknown");
          setKicadDetail("This build reported no kicad-cli entry at all.");
          return;
        }
        setKicad(tool.available ? "found" : "missing");
        setKicadDetail(tool.detail);
      })
      .catch(() => {
        if (live) setKicad("unknown");
      });
    return () => {
      live = false;
    };
  }, [probeTools]);

  // Only a real "found" completes the card. "unknown" is not an answer, and
  // recording it as done would put a machine nobody asked about into the
  // connected column.
  useEffect(() => {
    if (kicad === "checking") return;
    setCard?.("kicad", kicad === "found");
  }, [kicad, setCard]);

  // The key row, once per healthy transition.
  useEffect(() => {
    if (!health.ok) {
      setKey(null);
      setKeyChecked(false);
      return;
    }
    let cancelled = false;
    void fetchKeyStatus(baseUrl, token).then((status) => {
      if (cancelled) return;
      setKey(status);
      setKeyChecked(true);
    });
    return () => {
      cancelled = true;
    };
  }, [health.ok, baseUrl, token]);

  const allowed = health.ok && keyChecked && keyAllowsContinue(key);

  useEffect(() => {
    onCanContinue(allowed);
  }, [allowed, onCanContinue]);

  // A screen that self-dismisses in 600 ms cannot deliver bad news, so a
  // missing canvas holds it: the wizard only skates past when it has nothing
  // to tell you. This is OpenWhispr's `required-models` rule
  // (`vendor/openwhispr/src/components/onboarding/flow.ts:159-161`, where the
  // step is spliced into the route only when the dependency is absent),
  // applied inside an existing screen rather than as a seventh one.
  //
  // "checking" deliberately does not block: the probe is a local `which` that
  // starts on mount and settles long before this timer, and letting an
  // unanswered probe stall the wizard would be a worse failure than an
  // occasional early advance — the answer still reaches the Done screen's
  // skipped line and the Settings pane either way.
  useEffect(() => {
    if (!allowed || advanced.current || kicad === "missing") return;
    advanced.current = true;
    const timer = window.setTimeout(onAutoAdvance, AUTO_ADVANCE_MS);
    return () => window.clearTimeout(timer);
  }, [allowed, kicad, onAutoAdvance]);

  const probing = health.lastCheckedAt === null;
  const down = !probing && !health.ok;

  return (
    <StepFrame stepId="engine" title={down ? ENGINE_TITLE_DOWN : ENGINE_TITLE} subtitle={ENGINE_SUBTITLE}>
      <div className="space-y-4 text-left">
        <div
          className="flex items-start gap-3 rounded-lg border bg-card p-3"
          data-testid="engine-card"
          data-state={probing ? "probing" : health.ok ? "up" : "down"}
        >
          {probing ? (
            <Loader2Icon className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
          ) : health.ok ? (
            <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-hidden="true" />
          ) : (
            <XCircleIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <p className="text-sm font-medium" data-testid="engine-answer">
              {probing
                ? `Asking ${baseUrl} …`
                : health.ok
                  ? `Answered /healthz at ${baseUrl}`
                  : `Nothing answered at ${baseUrl}`}
            </p>
            {down && health.detail ? (
              <p className="text-xs text-muted-foreground">{health.detail}</p>
            ) : null}
            {health.ok ? (
              <p
                className={`text-xs ${key && !keyAllowsContinue(key) ? "text-destructive" : "text-muted-foreground"}`}
                data-testid="engine-key"
                data-state={key?.state ?? "checking"}
              >
                {!keyChecked
                  ? "Checking for a Gemini key…"
                  : key?.state === "ready"
                    ? `Gemini key: ${key.summary}`
                    : key?.summary || "Key state unknown."}
              </p>
            ) : null}
          </div>
          <Button size="sm" variant="ghost" onClick={health.recheck} disabled={health.checking} data-testid="engine-recheck">
            {health.checking ? "Checking…" : "Recheck"}
          </Button>
        </div>

        {down ? <EngineStartCommands compact only={["serve", "module"]} /> : null}

        <div
          className="flex items-start gap-3 rounded-lg border bg-card p-3"
          data-testid="kicad-card"
          data-state={kicad}
        >
          {kicad === "checking" ? (
            <Loader2Icon className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
          ) : kicad === "found" ? (
            <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600" aria-hidden="true" />
          ) : (
            <XCircleIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <p className="text-sm font-medium" data-testid="kicad-answer">
              {kicad === "checking"
                ? "Looking for KiCad…"
                : kicad === "found"
                  ? "KiCad is here"
                  : kicad === "missing"
                    ? "No KiCad on this Mac"
                    : "KiCad: not asked"}
            </p>
            <p className="text-xs text-muted-foreground" data-testid="kicad-detail">
              {kicad === "found"
                ? KICAD_FOUND_CAVEAT
                : kicad === "missing"
                  ? KICAD_MISSING_COST
                  : kicad === "unknown"
                    ? KICAD_UNKNOWN
                    : ""}
            </p>
            {kicad === "missing" ? (
              <p className="text-xs" data-testid="kicad-fix">
                {KICAD_MISSING_FIX}
              </p>
            ) : null}
            {kicadDetail && kicad !== "found" ? (
              <p className="text-[11px] text-muted-foreground" data-testid="kicad-probe-detail">
                {kicadDetail}
              </p>
            ) : null}
          </div>
        </div>

        <div className="text-center">
          <Button
            variant="link"
            size="sm"
            onClick={() => navigate("/engine?return=/welcome")}
            data-testid="engine-different-address"
          >
            Use a different address
          </Button>
        </div>
      </div>
    </StepFrame>
  );
};
