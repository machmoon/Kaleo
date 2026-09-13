import { AlertTriangleIcon, CheckIcon, Loader2Icon, MinusIcon } from "lucide-react";
import { Badge, Button } from "@/components";
import { cn } from "@/lib/utils";
import {
  badgeFor,
  type Integration,
  type IntegrationAction,
  type IntegrationState,
} from "@/lib/silkscreen/integrations";

/**
 * What each state means, in the user's terms.
 *
 * The distinction that matters is `unavailable` versus `unconfigured`, and it
 * is the reason this map exists rather than a single "not ready" line: one is
 * "the code for this is not in the engine you are talking to, and no amount of
 * environment variables will change that", the other is "the code is right
 * there and nobody has filled in the settings". They send the engineer to
 * completely different fixes, so they are never allowed to read the same.
 *
 * `ready` is deliberately hedged. The engine's own contract says the state is
 * a claim about configuration only — nothing here has placed a live call — and
 * a card that said "working" over an untested credential would be a lie the
 * first real send exposes.
 */
const STATE_NOTE: Record<IntegrationState, string> = {
  ready:
    "Everything required is set. That is a statement about configuration, not proof a live call has succeeded.",
  partial: "Some required settings are set and some are missing.",
  unconfigured: "Available in this engine, but nothing is configured yet.",
  unavailable:
    "Not available in this engine — the code is not installed here, so configuration cannot help until that changes.",
};

const TONE_CLASS: Record<"ok" | "warn" | "off", string> = {
  ok: "border-chart-2/40 bg-chart-2/10 text-chart-2",
  warn: "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400",
  off: "border-input/60 bg-muted/40 text-muted-foreground",
};

export interface IntegrationCardProps {
  item: Integration;
  /** True while this card's own action is running. */
  busy: boolean;
  /** True while any card's action is running — one in flight at a time. */
  locked: boolean;
  /** The last failure for this card, verbatim; undefined when none. */
  failure?: string;
  onAction: (item: Integration, action: IntegrationAction) => void;
}

/**
 * One integration: what it is, whether it is set up, and what to do if not.
 *
 * The hints are the engine's own words, printed verbatim and never rewritten.
 * The fix lives in the environment the service was started with — this window
 * cannot set an environment variable in another process, and paraphrasing the
 * hint would only put a second, staler copy of the instructions on screen.
 * `DeliverPanel` takes exactly the same stance.
 *
 * Settings show a key and whether it is set. Never a value, not even a tail:
 * the engine masks them for a reason and this card does not undo that.
 */
export const IntegrationCard = ({
  item,
  busy,
  locked,
  failure,
  onAction,
}: IntegrationCardProps) => {
  const badge = badgeFor(item);
  const dimmed = item.state === "unavailable";

  return (
    <div
      data-testid="integration-card"
      data-integration={item.id}
      data-state={item.state}
      className={cn(
        "flex flex-col gap-2.5 rounded-xl border border-input/50 p-3.5",
        dimmed && "bg-muted/20"
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3
              data-testid="integration-name"
              className={cn("text-sm font-medium", dimmed && "text-muted-foreground")}
            >
              {item.name}
            </h3>
            {/* Unverified is stated, never softened away. The repo keeps
                unbuilt and unproven work visible on purpose; a surface that
                has never run against a live account must say so where the
                engineer decides whether to trust it. */}
            {item.unverified && (
              <span
                data-testid="integration-unverified"
                data-integration={item.id}
                className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-400"
                title="Built offline against the documented API. It has never been run against a live account."
              >
                <AlertTriangleIcon className="size-2.5" />
                Unverified — never run live
              </span>
            )}
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {item.summary}
          </p>
        </div>
        <Badge
          data-testid="integration-badge"
          data-integration={item.id}
          data-tone={badge.tone}
          variant="outline"
          className={cn("shrink-0", TONE_CLASS[badge.tone])}
        >
          {badge.label}
        </Badge>
      </div>

      <p
        data-testid="integration-state-note"
        data-integration={item.id}
        className="text-[11px] leading-relaxed text-muted-foreground"
      >
        {STATE_NOTE[item.state]}
      </p>

      {item.detail && (
        <p
          data-testid="integration-detail"
          data-integration={item.id}
          className="text-[11px] leading-relaxed"
        >
          {item.detail}
        </p>
      )}

      {item.settings.length > 0 && (
        <ul
          data-testid="integration-settings"
          data-integration={item.id}
          className="flex flex-col gap-1"
        >
          {item.settings.map((setting) => (
            <li
              key={setting.key}
              data-testid="integration-setting"
              data-integration={item.id}
              data-key={setting.key}
              data-set={setting.set ? "yes" : "no"}
              className="flex items-baseline gap-2 text-[11px]"
            >
              {setting.set ? (
                <CheckIcon className="size-3 shrink-0 translate-y-0.5 text-chart-2" />
              ) : (
                <MinusIcon className="size-3 shrink-0 translate-y-0.5 text-muted-foreground" />
              )}
              <code className="font-mono break-all">{setting.key}</code>
              <span className="text-muted-foreground">
                {setting.set ? "set" : setting.required ? "not set (required)" : "not set (optional)"}
              </span>
              {/* `shown` is the engine's mask, e.g. `xoxb-…4f2a`. It is
                  reproduced as given and never reconstructed into a value. */}
              {setting.set && setting.shown && (
                <code className="font-mono text-muted-foreground break-all">
                  {setting.shown}
                </code>
              )}
              {setting.note && (
                <span className="text-muted-foreground">{setting.note}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {item.hints.length > 0 && (
        <ul className="flex flex-col gap-1">
          {item.hints.map((hint) => (
            <li
              key={hint}
              data-testid="integration-hint"
              data-integration={item.id}
              className="rounded-lg border border-input/40 bg-muted/30 px-2.5 py-1.5 text-[11px] leading-relaxed text-muted-foreground"
            >
              {hint}
            </li>
          ))}
        </ul>
      )}

      {(item.actions.length > 0 || item.docs) && (
        <div className="flex flex-wrap items-center gap-2">
          {item.actions.map((action) => (
            <Button
              key={action.id}
              size="sm"
              variant="outline"
              data-testid="integration-action"
              data-integration={item.id}
              data-action={action.id}
              // Locked while anything is in flight: every action here is a real
              // call against the engine, and a second click would be a second
              // one.
              disabled={locked}
              onClick={() => onAction(item, action)}
            >
              {busy && <Loader2Icon className="size-3.5 animate-spin" />}
              {action.label}
            </Button>
          ))}
          {item.docs && (
            <span className="text-[11px] text-muted-foreground">
              Documented in <code className="font-mono">{item.docs}</code>
            </span>
          )}
        </div>
      )}

      {failure && (
        <p
          data-testid="integration-failure"
          data-integration={item.id}
          className="text-[11px] leading-relaxed text-destructive break-all"
        >
          {failure}
        </p>
      )}
    </div>
  );
};
