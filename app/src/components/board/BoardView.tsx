import { useCallback, useMemo, useRef, useState } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { buildBoardGeometry, type PartGeometry } from "@/lib/silkscreen/board";
import type { Placements } from "@/lib/silkscreen/types";

/**
 * The board's own palette, deliberately fixed rather than themed: a board is a
 * board, and a soldermask that turns white in light mode is a lie about the
 * object. Everything *around* the board (chrome, labels, legend) uses the
 * app's CSS variables so both themes still work.
 *
 * Kept as custom properties in one place so the legend can paint the exact
 * same swatches by name instead of re-declaring hex literals.
 */
export const BOARD_COLOR_VARS = {
  "--pcb-substrate": "#0e2620",
  "--pcb-outline": "#f2c14e",
  "--pcb-courtyard": "#8fa2ff",
  "--pcb-courtyard-fill": "rgba(143, 162, 255, 0.10)",
  "--pcb-pad": "#d8a848",
  "--pcb-silk": "#e8efeb",
  "--pcb-selected": "#4de2f7",
  "--pcb-hover": "#ffffff",
} as const satisfies Record<string, string>;

const MIN_ZOOM = 0.4;
const MAX_ZOOM = 24;
/** Pointer travel (px) past which a drag stops counting as a click. */
const DRAG_SLOP_PX = 4;

interface Transform {
  k: number;
  tx: number;
  ty: number;
}

const IDENTITY: Transform = { k: 1, tx: 0, ty: 0 };

const clampZoom = (k: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));

export interface BoardViewProps {
  /** The run's `placements`, or undefined/null when no board exists yet. */
  placements?: Placements | null;
  /** Refs to highlight. Accepts a Set or an array so callers need not convert. */
  selected?: ReadonlySet<string> | readonly string[];
  /**
   * Called when a part is activated (click, Enter or Space). Receives the ref
   * and the part; selection state itself stays with the caller.
   */
  onSelectPart?: (ref: string, part: PartGeometry["part"]) => void;
  className?: string;
}

export function BoardView({
  placements,
  selected,
  onSelectPart,
  className,
}: BoardViewProps) {
  const [transform, setTransform] = useState<Transform>(IDENTITY);
  const [activeRef, setActiveRef] = useState<string | null>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const panRef = useRef<{ id: number; x: number; y: number; moved: number } | null>(
    null,
  );
  /** Travel of the gesture that just ended — read by the click that follows. */
  const lastTravelRef = useRef(0);

  const selectedSet = useMemo(
    () => (selected instanceof Set ? selected : new Set(selected ?? [])),
    [selected],
  );

  const geometry = useMemo(
    () => (placements ? buildBoardGeometry(placements) : null),
    [placements],
  );

  const zoomAt = useCallback((factor: number, px?: number, py?: number) => {
    setTransform((t) => {
      const k = clampZoom(t.k * factor);
      if (k === t.k) return t;
      const rect = surfaceRef.current?.getBoundingClientRect();
      // Zoom about the pointer when we have one, about the centre otherwise.
      const ax = px ?? (rect ? rect.width / 2 : 0);
      const ay = py ?? (rect ? rect.height / 2 : 0);
      const ratio = k / t.k;
      return { k, tx: ax - (ax - t.tx) * ratio, ty: ay - (ay - t.ty) * ratio };
    });
  }, []);

  const onWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      const rect = surfaceRef.current?.getBoundingClientRect();
      if (!rect) return;
      zoomAt(
        Math.exp(-e.deltaY * 0.0015),
        e.clientX - rect.left,
        e.clientY - rect.top,
      );
    },
    [zoomAt],
  );

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    panRef.current = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: 0 };
    lastTravelRef.current = 0;
    // Deliberately NO setPointerCapture here: with a capture active the
    // browser retargets pointerup — and the click composed from it — to the
    // capturing surface, so a part's own onClick can never fire and mouse
    // selection is dead. Capture starts only once the press has travelled
    // past the drag slop (below), where it is a pan and clicks stop
    // mattering.
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (!pan || pan.id !== e.pointerId) return;
    const dx = e.clientX - pan.x;
    const dy = e.clientY - pan.y;
    pan.x = e.clientX;
    pan.y = e.clientY;
    pan.moved += Math.abs(dx) + Math.abs(dy);
    if (pan.moved > DRAG_SLOP_PX && !e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.setPointerCapture(e.pointerId);
    }
    setTransform((t) => ({ ...t, tx: t.tx + dx, ty: t.ty + dy }));
  };

  const endPan = (e: React.PointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (!pan || pan.id !== e.pointerId) return;
    lastTravelRef.current = pan.moved;
    panRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  /** A click that ended a pan is a pan, not a selection. */
  const dragged = () => lastTravelRef.current > DRAG_SLOP_PX;

  const activate = (p: PartGeometry) => onSelectPart?.(p.ref, p.part);

  const hovered = activeRef ? geometry?.byRef.get(activeRef) : undefined;

  if (!geometry || geometry.parts.length === 0) {
    return (
      <div
        data-testid="board-empty"
        className={cn(
          "flex h-full min-h-48 w-full flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-border bg-card/40 p-6 text-center",
          className,
        )}
      >
        <p className="text-sm font-medium text-foreground">No board yet</p>
        <p className="text-xs text-muted-foreground">
          {geometry
            ? "The run returned placements with no parts in them."
            : "Generate a board and its placement will be drawn here."}
        </p>
      </div>
    );
  }

  const { width, height, parts, viewBox, flipTransform } = geometry;
  // Silkscreen refs scale with the board, not the zoom, so a big board does not
  // end up plastered in giant text.
  const labelSize = Math.max(0.7, Math.min(1.6, Math.min(width, height) / 28));

  return (
    <div
      className={cn(
        "flex h-full w-full flex-col overflow-hidden rounded-lg border border-border bg-card",
        className,
      )}
      style={BOARD_COLOR_VARS as React.CSSProperties}
    >
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex items-baseline gap-2 text-xs text-muted-foreground">
          <span data-testid="board-size" className="font-medium text-foreground">
            {width.toFixed(1)} × {height.toFixed(1)} mm
          </span>
          <span data-testid="board-part-count">
            {parts.length} {parts.length === 1 ? "part" : "parts"}
          </span>
          {selectedSet.size > 0 && <span>{selectedSet.size} selected</span>}
        </div>
        <div className="flex items-center gap-1">
          <Button
            data-testid="board-zoom-out"
            variant="ghost"
            size="icon"
            aria-label="Zoom out"
            onClick={() => zoomAt(1 / 1.3)}
          >
            <Minus className="size-4" />
          </Button>
          <Button
            data-testid="board-zoom-in"
            variant="ghost"
            size="icon"
            aria-label="Zoom in"
            onClick={() => zoomAt(1.3)}
          >
            <Plus className="size-4" />
          </Button>
          <Button
            data-testid="board-reset-view"
            variant="ghost"
            size="icon"
            aria-label="Reset view"
            onClick={() => setTransform(IDENTITY)}
          >
            <Maximize2 className="size-4" />
          </Button>
        </div>
      </div>

      <div
        ref={surfaceRef}
        data-testid="board-surface"
        className="relative flex-1 cursor-grab overflow-hidden active:cursor-grabbing"
        style={{ background: "var(--pcb-substrate)" }}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endPan}
        onPointerCancel={endPan}
      >
        <div
          className="h-full w-full origin-top-left"
          style={{
            transform: `translate(${transform.tx}px, ${transform.ty}px) scale(${transform.k})`,
          }}
        >
          <svg
            data-testid="board-svg"
            role="img"
            aria-label={`Printed circuit board, ${width.toFixed(1)} by ${height.toFixed(
              1,
            )} millimetres, ${parts.length} parts`}
            viewBox={viewBox}
            className="h-full w-full"
          >
            <title>
              {`Board ${width.toFixed(1)} × ${height.toFixed(1)} mm, ${parts.length} parts`}
            </title>

            {/* The one and only Y flip lives on this group. Nothing inside it
                may flip again, and nothing textual may live inside it. */}
            <g transform={flipTransform}>
              <rect
                x={0}
                y={0}
                width={width}
                height={height}
                fill="var(--pcb-substrate)"
                stroke="var(--pcb-outline)"
                strokeWidth={1.5}
                vectorEffect="non-scaling-stroke"
              />

              {parts.map((p, pi) => {
                const isSelected = selectedSet.has(p.ref);
                const isActive = activeRef === p.ref;
                const padCount = p.pads.length;
                return (
                  <g
                    key={`${p.ref}-${pi}`}
                    data-testid="board-part"
                    data-ref={p.ref}
                    data-selected={isSelected || undefined}
                    role="button"
                    tabIndex={0}
                    aria-pressed={isSelected}
                    aria-label={`${p.ref}, ${p.part.footprint}${
                      p.part.value ? `, ${p.part.value}` : ""
                    }, ${padCount} pads`}
                    className="cursor-pointer outline-none"
                    onClick={() => {
                      if (!dragged()) activate(p);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        activate(p);
                      }
                    }}
                    onPointerEnter={() => setActiveRef(p.ref)}
                    onPointerLeave={() =>
                      setActiveRef((r) => (r === p.ref ? null : r))
                    }
                    onFocus={() => setActiveRef(p.ref)}
                    onBlur={() => setActiveRef((r) => (r === p.ref ? null : r))}
                  >
                    <title>
                      {`${p.ref} · ${p.part.footprint}${
                        p.part.value ? ` · ${p.part.value}` : ""
                      } · ${padCount} ${padCount === 1 ? "pad" : "pads"}`}
                    </title>
                    <rect
                      x={p.courtyard.x}
                      y={p.courtyard.y}
                      width={p.courtyard.width}
                      height={p.courtyard.height}
                      fill={
                        isSelected
                          ? "color-mix(in srgb, var(--pcb-selected) 18%, transparent)"
                          : "var(--pcb-courtyard-fill)"
                      }
                      stroke={
                        isSelected
                          ? "var(--pcb-selected)"
                          : isActive
                            ? "var(--pcb-hover)"
                            : "var(--pcb-courtyard)"
                      }
                      strokeWidth={isSelected ? 2.5 : 1}
                      strokeDasharray={isSelected ? undefined : "4 3"}
                      vectorEffect="non-scaling-stroke"
                    />
                    {p.pads.map((pad, padIndex) => (
                      <rect
                        key={`${pad.number}-${padIndex}`}
                        x={pad.rect.x}
                        y={pad.rect.y}
                        width={pad.rect.width}
                        height={pad.rect.height}
                        fill="var(--pcb-pad)"
                        opacity={isSelected || isActive ? 1 : 0.85}
                      />
                    ))}
                  </g>
                );
              })}
            </g>

            {/* Labels sit OUTSIDE the flipped group: glyphs inside it render
                mirrored. board.ts already resolved these into this frame. */}
            <g pointerEvents="none">
              {parts.map((p, pi) => (
                <text
                  key={`${p.ref}-${pi}`}
                  data-testid="board-part-label"
                  data-ref={p.ref}
                  x={p.label.x}
                  y={p.label.y}
                  fontSize={labelSize}
                  textAnchor="middle"
                  dominantBaseline="central"
                  fill={
                    selectedSet.has(p.ref) ? "var(--pcb-selected)" : "var(--pcb-silk)"
                  }
                  style={{ fontFamily: "ui-monospace, monospace" }}
                >
                  {p.ref}
                </text>
              ))}
            </g>
          </svg>
        </div>
      </div>

      <div
        data-testid="board-hover-detail"
        className="flex min-h-8 items-center gap-2 border-t border-border px-3 py-1.5 text-xs text-muted-foreground"
      >
        {hovered ? (
          <>
            <span className="font-mono font-medium text-foreground">
              {hovered.ref}
            </span>
            <span className="truncate">{hovered.part.footprint}</span>
            {hovered.part.value && (
              <span className="truncate">{hovered.part.value}</span>
            )}
            <span className="ml-auto shrink-0">
              {hovered.pads.length}{" "}
              {hovered.pads.length === 1 ? "pad" : "pads"}
              {hovered.part.rotated ? " · rotated" : ""}
            </span>
          </>
        ) : (
          <span>Scroll to zoom, drag to pan. Click or press Enter on a part.</span>
        )}
      </div>
    </div>
  );
}

export default BoardView;
