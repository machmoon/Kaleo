import { useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import {
  LEVELS,
  LOG_NDJSON_MIME,
  LOG_TEXT_MIME,
  clearLog,
  getSnapshot,
  logFilename,
  subscribe,
  toNdjson,
  toText,
  type LogEntry,
  type LogLevel,
} from "@/lib/silkscreen/log";

/** Rows painted before the list is clipped; the buffer holds a thousand and
    repainting all of them on every append is what makes a drawer stutter. */
const VISIBLE = 300;
const MAX_DATA_CHARS = 300;
const COPY_FLASH_MS = 1200;

const LEVEL_TEXT: Record<LogLevel, string> = {
  error: "text-destructive",
  warn: "text-chart-5",
  info: "text-foreground",
  debug: "text-muted-foreground",
};

function clock(t: number): string {
  return new Date(Number(t) || 0).toISOString().slice(11, 23);
}

function dataText(entry: LogEntry): string {
  if (entry.data === null || entry.data === undefined) return "";
  let text: string;
  try {
    text = JSON.stringify(entry.data) ?? "";
  } catch {
    return "";
  }
  if (text === "{}" || text === "[]") return "";
  return text.length > MAX_DATA_CHARS ? `${text.slice(0, MAX_DATA_CHARS)}…` : text;
}

function download(text: string, filename: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * The one place low-level run metadata is allowed to surface: the most recent
 * server entry that carried `served_by` or `cache` (a `run.done` frame nests
 * them under `result`). Derived from the buffer, never invented.
 */
function runMeta(entries: readonly LogEntry[]) {
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.src !== "server") continue;
    const data = entry.data as Record<string, unknown> | null;
    if (!data || typeof data !== "object") continue;
    const holder = (
      typeof data.result === "object" && data.result !== null ? data.result : data
    ) as Record<string, unknown>;
    const servedBy = typeof holder.served_by === "string" ? holder.served_by : "";
    const cache =
      typeof holder.cache === "object" && holder.cache !== null
        ? (holder.cache as { hit?: string[]; read?: string[]; unusable?: string[] })
        : null;
    if (servedBy || cache) return { servedBy, cache };
  }
  return null;
}

export function DebugConsole({
  onClose,
  className,
  /**
   * `drawer` keeps the strip chrome (top border, inline "Console" label) for
   * an overlay panel. `page` drops both — the dashboard route already draws
   * a Header — and lets the list fill the remaining height.
   */
  chrome = "drawer",
}: {
  onClose?: () => void;
  className?: string;
  chrome?: "drawer" | "page";
}) {
  const { entries, dropped } = useSyncExternalStore(subscribe, getSnapshot);
  const [level, setLevel] = useState<"all" | LogLevel>("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy");
  const copyTimer = useRef(0);

  const errors = entries.filter((e) => e.level === "error").length;
  const warnings = entries.filter((e) => e.level === "warn").length;

  // The two filters compose: "errors only" narrows whatever the level select
  // left. They are view concerns — Copy and both exports always write the
  // whole buffer, because a file that mirrors a filter lies about the run.
  const shown = useMemo(() => {
    const byLevel = level === "all" ? entries : entries.filter((e) => e.level === level);
    return errorsOnly ? byLevel.filter((e) => e.level === "error") : byLevel;
  }, [entries, level, errorsOnly]);
  const rows = !showAll && shown.length > VISIBLE ? shown.slice(-VISIBLE) : shown;
  const hidden = shown.length - rows.length;

  const meta = useMemo(() => runMeta(entries), [entries]);

  const flash = (label: string) => {
    setCopyLabel(label);
    window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyLabel("Copy"), COPY_FLASH_MS);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(toText());
      flash("Copied");
    } catch {
      flash("Copy failed");
    }
  };

  return (
    <section
      aria-label="Debug console"
      data-testid="debug-console"
      data-chrome={chrome}
      className={cn(
        "flex min-h-0 flex-col bg-background text-xs",
        chrome === "drawer" && "border-t border-border",
        className,
      )}
    >
      <div
        className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-2 py-1.5"
        data-testid="debug-console-toolbar"
      >
        {chrome === "drawer" ? <span className="font-medium">Console</span> : null}
        <span
          className="font-mono text-[11px] text-muted-foreground"
          data-testid="debug-console-counts"
        >
          {entries.length} {entries.length === 1 ? "entry" : "entries"} · {errors}{" "}
          {errors === 1 ? "error" : "errors"} · {warnings}{" "}
          {warnings === 1 ? "warning" : "warnings"}
        </span>
        {dropped > 0 && (
          <span
            className="font-mono text-[11px] text-chart-5"
            data-testid="debug-console-dropped"
          >
            dropped {dropped}
          </span>
        )}

        <span className="grow" />

        <Select value={level} onValueChange={(v) => setLevel(v as "all" | LogLevel)}>
          <SelectTrigger
            size="sm"
            className="h-6 gap-1 px-2 text-[11px]"
            data-testid="debug-console-level"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">all levels</SelectItem>
            {LEVELS.map((l) => (
              <SelectItem key={l} value={l}>
                {l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          variant={errorsOnly ? "secondary" : "ghost"}
          size="sm"
          className="h-6 px-2 text-[11px]"
          aria-pressed={errorsOnly}
          onClick={() => setErrorsOnly((v) => !v)}
          data-testid="debug-console-filter-errors"
        >
          Errors only
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          onClick={copy}
          data-testid="debug-console-copy"
        >
          {copyLabel}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          onClick={() => download(toText(), logFilename("txt"), LOG_TEXT_MIME)}
          data-testid="debug-console-export-text"
        >
          Export .txt
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          onClick={() => download(toNdjson(), logFilename("ndjson"), LOG_NDJSON_MIME)}
          data-testid="debug-console-export-ndjson"
        >
          Export .ndjson
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-[11px]"
          onClick={() => {
            setShowAll(false);
            clearLog();
          }}
          data-testid="debug-console-clear"
        >
          <Trash2 className="size-3" />
          Clear
        </Button>
        {onClose && (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-2 text-[11px]"
            onClick={onClose}
            data-testid="debug-console-close"
          >
            Close
          </Button>
        )}
      </div>

      <ScrollArea className="min-h-0 grow">
        <ul className="font-mono text-[11px] leading-relaxed" data-testid="debug-console-list">
          {hidden > 0 && (
            <li className="border-b border-border px-3 py-1">
              <Button
                variant="ghost"
                size="sm"
                className="h-5 px-2 text-[11px]"
                onClick={() => setShowAll(true)}
                data-testid="debug-console-show-all"
              >
                Show all {shown.length}
              </Button>
            </li>
          )}

          {/* Keyed by seq, not index: head eviction renumbers every index. */}
          {rows.map((entry) => (
            <li
              key={entry.seq}
              className={cn(
                "flex items-baseline gap-2 border-b border-border/50 px-3 py-0.5",
                entry.level === "error" && "bg-destructive/10",
              )}
              data-testid="debug-console-entry"
              data-level={entry.level}
              data-src={entry.src}
              data-seq={entry.seq}
              data-event={entry.event}
            >
              <span className="shrink-0 text-muted-foreground">{clock(entry.t)}</span>
              <span
                className={cn(
                  "w-[5ch] shrink-0 tracking-wide",
                  LEVEL_TEXT[entry.level],
                )}
              >
                {entry.level.toUpperCase()}
              </span>
              <span className="shrink-0 text-muted-foreground">{entry.src}</span>
              {entry.event && (
                <span className="shrink-0 text-muted-foreground">{entry.event}</span>
              )}
              <span className={cn("[overflow-wrap:anywhere]", LEVEL_TEXT[entry.level])}>
                {entry.msg}
              </span>
              {dataText(entry) && (
                <span className="text-muted-foreground [overflow-wrap:anywhere]">
                  {dataText(entry)}
                </span>
              )}
            </li>
          ))}

          {shown.length === 0 && (
            <li
              className="px-3 py-3 font-sans text-muted-foreground whitespace-normal"
              data-testid="debug-console-empty"
            >
              {entries.length > 0
                ? "Nothing matches the current filter."
                : "Nothing captured yet. App events, stream frames, and errors appear here."}
            </li>
          )}
        </ul>
      </ScrollArea>

      {meta && (
        <div
          className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border px-3 py-1 font-mono text-[11px] text-muted-foreground"
          data-testid="debug-console-run-meta"
        >
          {meta.servedBy && <span>served_by {meta.servedBy}</span>}
          {meta.cache && (
            <span>
              cache hit {meta.cache.hit?.length ?? 0} · read{" "}
              {meta.cache.read?.length ?? 0} · unusable{" "}
              {meta.cache.unusable?.length ?? 0}
            </span>
          )}
        </div>
      )}
    </section>
  );
}
