import { useLocation, useNavigate } from "react-router-dom";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui";
import { BoardView, BoardLegend } from "@/components/board";
import { SchematicView, NetList } from "@/components/schematic";
import type { SchematicSelect } from "@/components/schematic";
// ReviewPanel renders WhatWasChecked itself, so the review tab is one panel.
import { ReviewPanel } from "@/components/review";
import { ArtifactPanel } from "@/components/artifacts";
import type { Finding, RunResult } from "@/lib/silkscreen/types";

const TAB_IDS = ["board", "schematic", "review", "artifacts"] as const;
type TabId = (typeof TAB_IDS)[number];

const TAB_LABELS: Record<TabId, string> = {
  board: "Board",
  schematic: "Schematic",
  review: "Review",
  artifacts: "Artifacts",
};

export interface WorkbenchTabsProps {
  result: RunResult;
  /** The submitted request's flags, so a skipped review reads as skipped. */
  reviewRequested?: boolean;
  grounded?: boolean;
  /** The submitted intent; ArtifactPanel uses it to name the saved board. */
  intent?: string;
  selectedRefs: string[];
  selectedFindingId: string | null;
  selectedNet: string | null;
  onSelectPart: (ref: string) => void;
  onSelectFinding: (refs: string[], finding: Finding | null) => void;
  onSchematicSelect: SchematicSelect;
}

export function WorkbenchTabs({
  result,
  reviewRequested,
  grounded,
  intent,
  selectedRefs,
  selectedFindingId,
  selectedNet,
  onSelectPart,
  onSelectFinding,
  onSchematicSelect,
}: WorkbenchTabsProps) {
  // The active tab lives in the URL hash so switching stays a pure hash
  // change — the router never remounts the page, and reopening the window
  // comes back to the same tab. An unknown hash falls back to Board.
  const location = useLocation();
  const navigate = useNavigate();
  const fromHash = location.hash.replace(/^#/, "");
  const tab: TabId = (TAB_IDS as readonly string[]).includes(fromHash)
    ? (fromHash as TabId)
    : "board";

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => navigate({ hash: value }, { replace: true })}
    >
      <TabsList className="w-full">
        {TAB_IDS.map((id) => (
          <TabsTrigger key={id} value={id}>
            {TAB_LABELS[id]}
          </TabsTrigger>
        ))}
      </TabsList>

      {/* forceMount keeps every pane alive across switches, so board pan/zoom
          and review scroll positions survive; inactive panes just hide. */}
      <TabsContent
        value="board"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <div className="flex flex-col gap-3" data-testid="workbench-tab-board">
          <BoardView
            placements={result.placements}
            selected={selectedRefs}
            onSelectPart={(ref) => onSelectPart(ref)}
            className="h-[55vh] min-h-80"
          />
          <BoardLegend />
        </div>
      </TabsContent>

      <TabsContent
        value="schematic"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <div
          className="flex flex-col gap-3 lg:flex-row"
          data-testid="workbench-tab-schematic"
        >
          <SchematicView
            schematic={result.schematic}
            selectedRefs={selectedRefs}
            selectedNet={selectedNet}
            onSelect={onSchematicSelect}
            className="min-h-80 flex-1"
          />
          <NetList
            schematic={result.schematic}
            selectedRefs={selectedRefs}
            selectedNet={selectedNet}
            onSelect={onSchematicSelect}
            className="lg:w-72"
          />
        </div>
      </TabsContent>

      <TabsContent
        value="review"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <div className="flex flex-col gap-3" data-testid="workbench-tab-review">
          <ReviewPanel
            findings={result.findings}
            warnings={result.warnings}
            blockers={result.blockers}
            selectedFindingId={selectedFindingId}
            onSelectFinding={onSelectFinding}
            reviewRequested={reviewRequested}
            datasheets={result.datasheets}
            grounded={grounded}
          />
        </div>
      </TabsContent>

      <TabsContent
        value="artifacts"
        forceMount
        className="data-[state=inactive]:hidden"
      >
        <div
          className="flex flex-col gap-3"
          data-testid="workbench-tab-artifacts"
        >
          <ArtifactPanel result={result} intent={intent} />
        </div>
      </TabsContent>
    </Tabs>
  );
}
