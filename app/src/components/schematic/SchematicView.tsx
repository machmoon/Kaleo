import { useMemo } from "react";
import { Workflow } from "lucide-react";
import type { Schematic, SchematicNet } from "@/lib/silkscreen/types";
import {
  EmptyComponent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { ScrollArea, ScrollBar } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { isPowerNet, netRefs, type SchematicSelect } from "./NetList";

// This is a CONNECTIVITY diagram, not schematic capture. No symbol glyphs, no
// wire routing, no attempt at a draughtsman's sheet — KiCad owns that, and a
// bad imitation of it reads as a real schematic while being wrong. What this
// draws is the one thing the response actually contains: which pin joins which
// net. Parts on the left with a row per pin, nets on the right, one line per
// terminal.
//
// The layout is DETERMINISTIC by construction: no randomness, no physics, no
// iteration to a fixed point. Parts keep the engine's order; nets are placed by
// a single barycentre pass (the mean height of the pins they touch) with the
// engine's order breaking every tie, so the same circuit lays out identically
// every time.

const PAD = 12;
const PART_W = 168;
const NET_W = 176;
const GAP = 168;
const ROW_H = 18;
const PART_HEADER_H = 24;
const PART_BODY_PAD = 4;
const PART_GAP = 14;
const NET_H = 26;
const NET_GAP = 10;

const NET_X = PAD + PART_W + GAP;

export interface PinRow {
  partId: string;
  ref: string;
  pin: string;
  number?: string | null;
  y: number;
  connected: boolean;
}

export interface PartBox {
  id: string;
  ref: string;
  label: string;
  kind?: string;
  y: number;
  height: number;
  pins: PinRow[];
  /** True when the part appeared only as a net endpoint, never in `parts`. */
  inferred: boolean;
}

export interface NetBox {
  net: SchematicNet;
  name: string;
  y: number;
  power: boolean;
  refs: string[];
}

export interface Edge {
  key: string;
  net: string;
  ref: string;
  pin: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface SchematicLayout {
  parts: PartBox[];
  nets: NetBox[];
  edges: Edge[];
  width: number;
  height: number;
}

/** Pure layout. Same input, same output — see the determinism note above. */
export function layoutSchematic(schematic: Schematic): SchematicLayout {
  const nets = schematic.nets ?? [];

  // Endpoints are the authority on what exists. A part or a pin that only ever
  // shows up in a net still gets drawn, because dropping it would silently hide
  // connectivity the service did report.
  const pinsByPart = new Map<string, string[]>();
  const numbers = new Map<string, string | null | undefined>();
  const order: string[] = [];
  const refFor = new Map<string, string>();

  const touch = (partId: string) => {
    if (!pinsByPart.has(partId)) {
      pinsByPart.set(partId, []);
      order.push(partId);
    }
    return pinsByPart.get(partId)!;
  };

  for (const part of schematic.parts ?? []) {
    const pins = touch(part.id);
    if (part.ref) refFor.set(part.id, part.ref);
    for (const pin of part.pins ?? []) {
      if (!pins.includes(pin.name)) pins.push(pin.name);
      numbers.set(`${part.id}.${pin.name}`, pin.number);
    }
  }

  const connected = new Set<string>();
  for (const net of nets) {
    for (const endpoint of net.endpoints ?? []) {
      const pins = touch(endpoint.part_id);
      if (endpoint.ref) refFor.set(endpoint.part_id, endpoint.ref);
      if (!pins.includes(endpoint.pin)) pins.push(endpoint.pin);
      const key = `${endpoint.part_id}.${endpoint.pin}`;
      if (!numbers.has(key)) numbers.set(key, endpoint.number);
      connected.add(key);
    }
  }

  const declared = new Map(
    (schematic.parts ?? []).map((part) => [part.id, part] as const)
  );

  const parts: PartBox[] = [];
  const pinY = new Map<string, number>();
  let y = PAD;
  for (const id of order) {
    const pinNames = pinsByPart.get(id) ?? [];
    const spec = declared.get(id);
    const ref = refFor.get(id) ?? id;
    const height =
      PART_HEADER_H + PART_BODY_PAD * 2 + Math.max(pinNames.length, 1) * ROW_H;
    const pins: PinRow[] = pinNames.map((pin, index) => {
      const rowY =
        y + PART_HEADER_H + PART_BODY_PAD + index * ROW_H + ROW_H / 2;
      pinY.set(`${id}.${pin}`, rowY);
      return {
        partId: id,
        ref,
        pin,
        number: numbers.get(`${id}.${pin}`),
        y: rowY,
        connected: connected.has(`${id}.${pin}`),
      };
    });
    parts.push({
      id,
      ref,
      // The board calls it C3; the spec calls it `bulk_cap`. Show both when
      // they differ, since findings are written in the spec's vocabulary.
      label: spec?.value && spec.value !== id ? `${ref} · ${spec.value}` : ref,
      kind: spec?.kind,
      y,
      height,
      pins,
      inferred: spec === undefined,
    });
    y += height + PART_GAP;
  }
  const partsBottom = y - PART_GAP + PAD;

  // Barycentre: place each net beside the average height of the pins it joins.
  // One pass, no iteration — cheap, and it cannot converge differently twice.
  const ranked = nets.map((net, index) => {
    const ys = (net.endpoints ?? [])
      .map((endpoint) => pinY.get(`${endpoint.part_id}.${endpoint.pin}`))
      .filter((value): value is number => value !== undefined);
    const mean = ys.length
      ? ys.reduce((a, b) => a + b, 0) / ys.length
      : Number.POSITIVE_INFINITY;
    return { net, index, mean };
  });
  ranked.sort((a, b) => a.mean - b.mean || a.index - b.index);

  const netBoxes: NetBox[] = ranked.map((entry, position) => ({
    net: entry.net,
    name: entry.net.name,
    y: PAD + position * (NET_H + NET_GAP),
    power: isPowerNet(entry.net.name, (entry.net.endpoints ?? []).length),
    refs: netRefs(entry.net),
  }));
  const netsBottom = netBoxes.length
    ? PAD + netBoxes.length * (NET_H + NET_GAP) - NET_GAP + PAD
    : PAD * 2;

  const edges: Edge[] = [];
  for (const box of netBoxes) {
    for (const endpoint of box.net.endpoints ?? []) {
      const y1 = pinY.get(`${endpoint.part_id}.${endpoint.pin}`);
      if (y1 === undefined) continue;
      edges.push({
        key: `${box.name}::${endpoint.part_id}.${endpoint.pin}`,
        net: box.name,
        ref: endpoint.ref ?? endpoint.part_id,
        pin: endpoint.pin,
        x1: PAD + PART_W,
        y1,
        x2: NET_X,
        y2: box.y + NET_H / 2,
      });
    }
  }

  return {
    parts,
    nets: netBoxes,
    edges,
    width: NET_X + NET_W + PAD,
    height: Math.max(partsBottom, netsBottom),
  };
}

/** SVG text does not wrap or ellipsize, so long names are cut here instead. */
function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function edgePath(edge: Edge): string {
  const dx = (edge.x2 - edge.x1) / 2;
  return `M ${edge.x1} ${edge.y1} C ${edge.x1 + dx} ${edge.y1}, ${
    edge.x2 - dx
  } ${edge.y2}, ${edge.x2} ${edge.y2}`;
}

export interface SchematicViewProps {
  /** The response's `schematic` block. Undefined/null renders "not available". */
  schematic?: Schematic | null;
  /** Board refs to highlight, typically shared with the board view. */
  selectedRefs?: string[];
  /** Net name to highlight, if a net is the current selection. */
  selectedNet?: string | null;
  onSelect?: SchematicSelect;
  className?: string;
}

export function SchematicView({
  schematic,
  selectedRefs = [],
  selectedNet = null,
  onSelect,
  className,
}: SchematicViewProps) {
  const layout = useMemo(
    () => (schematic ? layoutSchematic(schematic) : null),
    [schematic]
  );

  if (!layout || (layout.parts.length === 0 && layout.nets.length === 0)) {
    return (
      <EmptyComponent className={cn("border", className)}>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Workflow />
          </EmptyMedia>
          <EmptyTitle>No schematic available</EmptyTitle>
          <EmptyDescription>
            {schematic
              ? "This run's schematic block carried no parts or nets, so there is nothing to connect."
              : "This run returned no schematic block. The circuit's connectivity was not reported — it is unknown, not empty."}
          </EmptyDescription>
        </EmptyHeader>
      </EmptyComponent>
    );
  }

  const isActiveRef = (ref: string) => selectedRefs.includes(ref);
  const isActiveNet = (box: NetBox) =>
    selectedNet === box.name || box.refs.some(isActiveRef);

  return (
    <div className={cn("flex min-h-0 flex-col gap-2", className)}>
      <p className="text-muted-foreground text-xs" data-testid="schematic-caption">
        Connectivity only — pins on the left, nets on the right. This is not a
        schematic sheet; open the emitted <code>.kicad_sch</code> in KiCad for
        that.
      </p>
      <ScrollArea className="min-h-0 flex-1 rounded-md border">
        <svg
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          className="block"
          data-testid="schematic-svg"
        >
          <g>
            {layout.edges.map((edge) => {
              const active =
                selectedNet === edge.net || isActiveRef(edge.ref);
              return (
                <path
                  key={edge.key}
                  d={edgePath(edge)}
                  fill="none"
                  className={cn(
                    "stroke-border",
                    active && "stroke-ring"
                  )}
                  strokeWidth={active ? 1.75 : 1}
                  data-testid="schematic-edge"
                  data-net={edge.net}
                  data-ref={edge.ref}
                  data-pin={edge.pin}
                />
              );
            })}
          </g>

          <g>
            {layout.parts.map((part) => {
              const active = isActiveRef(part.ref);
              return (
                <g
                  key={part.id}
                  data-testid="schematic-part"
                  data-ref={part.ref}
                  onClick={() => onSelect?.({ refs: [part.ref], net: null })}
                  className="cursor-pointer"
                >
                  <rect
                    x={PAD}
                    y={part.y}
                    width={PART_W}
                    height={part.height}
                    rx={6}
                    className={cn(
                      "fill-card stroke-border",
                      active && "stroke-ring"
                    )}
                    strokeWidth={active ? 1.75 : 1}
                  />
                  <text
                    x={PAD + 8}
                    y={part.y + 16}
                    className="fill-foreground text-[11px] font-medium"
                  >
                    {truncate(part.label, 18)}
                  </text>
                  {part.kind && (
                    <text
                      x={PAD + PART_W - 8}
                      y={part.y + 16}
                      textAnchor="end"
                      className="fill-muted-foreground text-[10px]"
                    >
                      {part.inferred ? `${part.kind}?` : part.kind}
                    </text>
                  )}
                  {part.pins.map((row) => (
                    <g key={`${part.id}.${row.pin}`}>
                      <text
                        x={PAD + 8}
                        y={row.y + 3}
                        className={cn(
                          "text-[10px]",
                          row.connected
                            ? "fill-foreground"
                            : "fill-muted-foreground"
                        )}
                        data-testid="schematic-pin"
                        data-ref={part.ref}
                        data-pin={row.pin}
                      >
                        {row.pin}
                        {row.number && row.number !== row.pin
                          ? ` (${row.number})`
                          : ""}
                        {row.connected ? "" : " · unconnected"}
                      </text>
                      {row.connected && (
                        <circle
                          cx={PAD + PART_W}
                          cy={row.y}
                          r={2.5}
                          className={cn(
                            "fill-border",
                            active && "fill-ring"
                          )}
                        />
                      )}
                    </g>
                  ))}
                </g>
              );
            })}
          </g>

          <g>
            {layout.nets.map((box) => {
              const active = isActiveNet(box);
              return (
                <g
                  key={box.name}
                  data-testid="schematic-net"
                  data-net={box.name}
                  onClick={() => onSelect?.({ refs: box.refs, net: box.name })}
                  className="cursor-pointer"
                >
                  <rect
                    x={NET_X}
                    y={box.y}
                    width={NET_W}
                    height={NET_H}
                    rx={13}
                    className={cn(
                      box.power ? "fill-muted" : "fill-card",
                      "stroke-border",
                      active && "stroke-ring"
                    )}
                    strokeWidth={active ? 1.75 : 1}
                  />
                  <text
                    x={NET_X + 12}
                    y={box.y + NET_H / 2 + 3.5}
                    className="fill-foreground text-[11px]"
                  >
                    {truncate(box.name, 20)}
                    {box.power ? " · power" : ""}
                  </text>
                  <text
                    x={NET_X + NET_W - 10}
                    y={box.y + NET_H / 2 + 3.5}
                    textAnchor="end"
                    className="fill-muted-foreground text-[10px]"
                  >
                    {(box.net.endpoints ?? []).length}
                  </text>
                  <circle
                    cx={NET_X}
                    cy={box.y + NET_H / 2}
                    r={2.5}
                    className={cn("fill-border", active && "fill-ring")}
                  />
                </g>
              );
            })}
          </g>
        </svg>
        <ScrollBar orientation="horizontal" />
      </ScrollArea>
    </div>
  );
}
