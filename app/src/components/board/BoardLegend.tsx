import { cn } from "@/lib/utils";
import { BOARD_COLOR_VARS } from "./BoardView";

interface Entry {
  label: string;
  hint: string;
  swatch: React.ReactNode;
}

/**
 * What the board well's colours and line styles mean. The swatches paint from
 * the same fixed board palette as the renderer, so the legend cannot drift out
 * of sync with what is drawn; the surrounding chrome stays themed.
 */
const ENTRIES: Entry[] = [
  {
    label: "Board outline",
    hint: "Edge.Cuts, the board's own boundary",
    swatch: (
      <span
        className="block h-3 w-6 rounded-[2px] border-2"
        style={{
          borderColor: "var(--pcb-outline)",
          background: "var(--pcb-substrate)",
        }}
      />
    ),
  },
  {
    label: "Courtyard",
    hint: "dashed — the keep-out the placer reserved",
    swatch: (
      <span
        className="block h-3 w-6 rounded-[2px] border-2 border-dashed"
        style={{
          borderColor: "var(--pcb-courtyard)",
          background: "var(--pcb-courtyard-fill)",
        }}
      />
    ),
  },
  {
    label: "Pad",
    hint: "copper the engine reported, one rect per pad",
    swatch: (
      <span
        className="block h-3 w-6 rounded-[1px]"
        style={{ background: "var(--pcb-pad)" }}
      />
    ),
  },
  {
    label: "Selected",
    hint: "solid — the part a finding names, or one you clicked",
    swatch: (
      <span
        className="block h-3 w-6 rounded-[2px] border-2"
        style={{
          borderColor: "var(--pcb-selected)",
          background: "color-mix(in srgb, var(--pcb-selected) 18%, transparent)",
        }}
      />
    ),
  },
  {
    label: "Reference",
    hint: "silkscreen designator, drawn upright over the part",
    swatch: (
      <span
        className="flex h-3 w-6 items-center justify-center rounded-[2px] font-mono text-[8px] leading-none"
        style={{ background: "var(--pcb-substrate)", color: "var(--pcb-silk)" }}
      >
        U1
      </span>
    ),
  },
];

export interface BoardLegendProps {
  className?: string;
}

export function BoardLegend({ className }: BoardLegendProps) {
  return (
    <ul
      data-testid="board-legend"
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground",
        className,
      )}
      style={BOARD_COLOR_VARS as React.CSSProperties}
    >
      {ENTRIES.map((e) => (
        <li
          key={e.label}
          data-testid="board-legend-item"
          data-label={e.label}
          className="flex items-center gap-1.5"
          title={e.hint}
        >
          {e.swatch}
          <span className="text-foreground">{e.label}</span>
          <span className="hidden sm:inline">{e.hint}</span>
        </li>
      ))}
    </ul>
  );
}

export default BoardLegend;
