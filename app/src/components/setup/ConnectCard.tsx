import type { ReactNode } from "react";
import { CheckIcon, Loader2Icon } from "lucide-react";
import { Button } from "@/components/ui";
import { DemoBadge } from "./DemoBadge";

export type ConnectCardState =
  /** Nothing configured; the primary button connects. */
  | "unconfigured"
  /** A sign-in is waiting in the browser, or a save is in flight. */
  | "connecting"
  /** Configured. The engine's own word; never "verified" unless it says so. */
  | "ready"
  /** Configured but not proven (no token yet, key row missing). */
  | "partial"
  /** The engine cannot offer this one; `detail` says why. */
  | "unavailable"
  /** The engine itself could not be asked. */
  | "down";

interface ConnectCardProps {
  id: string;
  name: string;
  /** One line under the name: what connecting it is for. */
  description: string;
  state: ConnectCardState;
  demo?: boolean;
  /** The engine's reason or status sentence, under the description. */
  detail?: string;
  /** The label on the primary button, when one makes sense. */
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
  /** The secondary (Disconnect) button, when ready. */
  secondaryLabel?: string;
  onSecondary?: () => void;
  /** Fields, sentences, anything below the header row. */
  children?: ReactNode;
  /** The word next to the check when ready; the engine's own ("Token issued"). */
  readyWord?: string;
}

/**
 * One account or permission, one card. The status word is the engine's
 * vocabulary (`ready|partial|unconfigured|unavailable`) plus the two states
 * only the client can know: connecting, and the engine being down.
 */
export const ConnectCard = ({
  id,
  name,
  description,
  state,
  demo = false,
  detail,
  actionLabel,
  onAction,
  actionDisabled = false,
  secondaryLabel,
  onSecondary,
  children,
  readyWord = "Connected",
}: ConnectCardProps) => (
  <div
    className="space-y-2 rounded-lg border bg-card p-3 text-left"
    data-testid="connect-card"
    data-id={id}
    data-state={state}
  >
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{name}</span>
          {demo ? <DemoBadge /> : null}
          {state === "ready" ? (
            <span
              className="inline-flex items-center gap-1 text-xs text-muted-foreground"
              data-testid="connect-ready"
            >
              <CheckIcon className="size-3.5" aria-hidden="true" />
              {readyWord}
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
        {detail ? (
          <p className="mt-1 text-xs text-muted-foreground" data-testid="connect-detail">
            {detail}
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {state === "connecting" ? (
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" data-testid="connect-waiting">
            <Loader2Icon className="size-3.5 animate-spin" aria-hidden="true" />
            Waiting for you…
          </span>
        ) : null}
        {state === "ready" && secondaryLabel && onSecondary ? (
          <Button size="sm" variant="outline" onClick={onSecondary} data-testid="connect-secondary">
            {secondaryLabel}
          </Button>
        ) : null}
        {state !== "ready" && state !== "connecting" && actionLabel && onAction ? (
          <Button
            size="sm"
            variant="outline"
            onClick={onAction}
            disabled={actionDisabled || state === "unavailable" || state === "down"}
            data-testid="connect-action"
          >
            {actionLabel}
          </Button>
        ) : null}
      </div>
    </div>
    {children}
  </div>
);
