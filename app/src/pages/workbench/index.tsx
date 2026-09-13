import { useCallback, useEffect, useState } from "react";
import { PageLayout } from "@/layouts";
// Imported from the context module directly rather than the `@/contexts`
// barrel — shared barrels are wired by the orchestrator, not here.
import { useSilkscreenRun } from "@/contexts/run.context";
import { findingKey } from "@/components/review";
import type { SchematicSelection } from "@/components/schematic";
// RunSummary is real; the barrel `index.ts` is the artifacts agent's to write.
import { RunSummary } from "@/components/artifacts";
import { DEFAULT_SHORTCUT_ACTIONS } from "@/config/shortcuts";
import { getPlatform } from "@/lib/platform";
import { cn } from "@/lib/utils";
import type { Finding } from "@/lib/silkscreen/types";
import { WorkbenchTabs } from "./components";

/** "cmd+backslash" -> "Cmd+\", in the current platform's binding. */
const shortcutLabel = (actionId: string): string | null => {
  const action = DEFAULT_SHORTCUT_ACTIONS.find((a) => a.id === actionId);
  if (!action) return null;
  const key = action.defaultKey[getPlatform()];
  if (!key) return null;
  const names: Record<string, string> = {
    cmd: "Cmd",
    ctrl: "Ctrl",
    shift: "Shift",
    alt: "Alt",
    backslash: "\\",
  };
  return key
    .split("+")
    .map((part) => names[part] ?? part.toUpperCase())
    .join("+");
};

const Workbench = () => {
  const {
    status,
    result,
    error,
    history,
    viewingId,
    selectRun,
    submitted,
  } = useSilkscreenRun();

  // Put the newest published run on the bench when nothing else is.
  //
  // This window is usually CREATED by a run reaching the point where it is
  // worth looking at, so the announcement it came from was written before
  // this provider existed: the bridge subscription restores it into `history`
  // on mount but deliberately does not put it on screen, since a `storage`
  // event that arrives while a window is already open is the only case that
  // knows the user is watching. Without this the dashboard opened with its
  // empty state over a run that had finished seconds earlier. Nothing is ever
  // live in this window — the dashboard starts no runs — so there is nothing
  // for this to interrupt, and `selectRun` refuses anyway while one is.
  useEffect(() => {
    if (result || viewingId || history.length === 0) return;
    selectRun(history[0].id);
  }, [result, viewingId, history, selectRun]);

  // Cross-highlight state lives here, above every tab, so a selection made in
  // one pane survives switching to another. Plain lifted state on purpose.
  const [selectedRefs, setSelectedRefs] = useState<string[]>([]);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(
    null
  );
  const [selectedNet, setSelectedNet] = useState<string | null>(null);

  const clearSelection = useCallback(() => {
    setSelectedRefs([]);
    setSelectedFindingId(null);
    setSelectedNet(null);
  }, []);

  // A different run taking the bench drops the selection — refs from one
  // board mean nothing on another. `result` identity is the switch signal,
  // whether the change came from a finished run or from the history rail.
  useEffect(() => {
    setSelectedRefs([]);
    setSelectedFindingId(null);
    setSelectedNet(null);
  }, [result]);

  const handleSelectFinding = useCallback(
    (refs: string[], finding: Finding | null) => {
      const index = finding ? (result?.findings ?? []).indexOf(finding) : -1;
      const id = finding ? findingKey(finding, index) : null;
      if (id === null || id === selectedFindingId) {
        clearSelection();
        return;
      }
      setSelectedFindingId(id);
      setSelectedRefs(refs ?? []);
      setSelectedNet(null);
    },
    [result, selectedFindingId, clearSelection]
  );

  const handleSelectPart = useCallback(
    (ref: string) => {
      // Clicking the sole selected part again reads as deselect.
      if (selectedRefs.length === 1 && selectedRefs[0] === ref) {
        clearSelection();
        return;
      }
      setSelectedRefs([ref]);
      setSelectedFindingId(null);
      setSelectedNet(null);
    },
    [selectedRefs, clearSelection]
  );

  const handleSchematicSelect = useCallback(
    (selection: SchematicSelection) => {
      const { refs, net } = selection;
      if ((net !== null && net === selectedNet) || (!net && refs.length === 0)) {
        clearSelection();
        return;
      }
      setSelectedNet(net);
      setSelectedRefs(refs);
      setSelectedFindingId(null);
    },
    [selectedNet, clearSelection]
  );

  const overlayHotkey = shortcutLabel("toggle_window");

  return (
    <PageLayout
      title="Workbench"
      description="Review a finished run — board, schematic, findings and artifacts."
    >
      {history.length > 1 && (
        <div
          className="flex flex-wrap items-center gap-2"
          data-testid="workbench-history"
        >
          <span className="text-xs text-muted-foreground">This session</span>
          {history.map((entry, i) => {
            const active = viewingId ? entry.id === viewingId : i === 0;
            return (
              <button
                key={entry.id}
                type="button"
                data-testid="workbench-history-run"
                data-run-id={entry.id}
                onClick={() => selectRun(i === 0 ? null : entry.id)}
                className={cn(
                  "max-w-56 truncate rounded-lg border px-2.5 py-1 text-xs transition-colors",
                  active
                    ? "border-primary bg-accent text-accent-foreground"
                    : "bg-card text-muted-foreground hover:text-foreground"
                )}
                title={entry.request.intent}
              >
                {entry.request.intent.trim() || `Run ${history.length - i}`}
              </button>
            );
          })}
        </div>
      )}

      {result === null ? (
        <div
          className="flex flex-col items-center gap-2 rounded-xl border border-dashed py-16 text-center"
          data-testid="workbench-empty"
        >
          {status === "running" ? (
            <>
              <p className="text-sm font-medium">A run is in progress</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                The overlay is driving it. The result lands here the moment
                the engine finishes.
              </p>
            </>
          ) : (
            <>
              <p className="text-sm font-medium">No board on the bench yet</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                Describe a board from the overlay bar to start a run
                {overlayHotkey ? (
                  <>
                    {" — "}
                    <kbd className="rounded border bg-muted px-1.5 py-0.5 font-mono text-xs">
                      {overlayHotkey}
                    </kbd>{" "}
                    brings it up
                  </>
                ) : null}
                . The finished board comes back to this window.
              </p>
              {status === "error" && error ? (
                <p className="max-w-sm text-sm text-destructive">
                  The last attempt failed: {error.message}
                </p>
              ) : null}
            </>
          )}
        </div>
      ) : (
        <>
          {status === "running" && (
            <p
              className="text-xs text-muted-foreground"
              data-testid="workbench-running-note"
            >
              A new run is in progress — this view still shows the last
              finished one.
            </p>
          )}

          <RunSummary result={result} />

          <WorkbenchTabs
            result={result}
            reviewRequested={submitted?.review}
            grounded={submitted?.ground}
            intent={submitted?.intent}
            selectedRefs={selectedRefs}
            selectedFindingId={selectedFindingId}
            selectedNet={selectedNet}
            onSelectPart={handleSelectPart}
            onSelectFinding={handleSelectFinding}
            onSchematicSelect={handleSchematicSelect}
          />
        </>
      )}
    </PageLayout>
  );
};

export default Workbench;
