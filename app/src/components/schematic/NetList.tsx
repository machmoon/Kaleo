import { useMemo, useState } from "react";
import { Zap } from "lucide-react";
import type { Schematic, SchematicNet, SchematicPin } from "@/lib/silkscreen/types";
import { Badge } from "@/components/ui/badge";
import {
  EmptyComponent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

/** One entry of `Schematic["parts"]`, named so props can talk about it. */
export type SchematicPart = NonNullable<Schematic["parts"]>[number];

/**
 * What a click in either schematic view means.
 *
 * `refs` is what the board well cross-highlights, so it carries board
 * reference designators — the identity both views share. `net` is set only
 * when a net itself was selected; clicking a part clears it.
 */
export interface SchematicSelection {
  refs: string[];
  net: string | null;
}

export type SchematicSelect = (selection: SchematicSelection) => void;

/**
 * Mirrors `is_power_net` in `engine/silkscreen/kicad.py` — the same patterns,
 * the same voltage-rail regex, the same fanout cutoff (`DEFAULT_MAX_NET_FANOUT`).
 * Two copies of one heuristic drift, so they must be kept in step: if the
 * engine's list changes, this changes with it. Inventing a different rule here
 * would mark a different set of nets as rails than the placer down-weighted,
 * which is worse than no marking at all.
 *
 * One deliberate difference: the engine counts *pads* on the routed board,
 * while the schematic block only knows endpoints. For a two-terminal passive
 * those agree; for a device pin mapped to several pads they can differ, so a
 * borderline net may be marked here and not there.
 */
const POWER_NET_PATTERNS = new Set([
  "gnd", "agnd", "dgnd", "pgnd", "vss", "avss", "dvss", "earth",
  "vcc", "vdd", "avdd", "dvdd", "vbus", "vin", "vout", "vref",
]);

const VOLTAGE_RAIL_RE = /^[+-]?\d+v\d*$/;

const DEFAULT_MAX_NET_FANOUT = 6;

export function isPowerNet(
  name: string,
  terminalCount: number,
  maxFanout: number = DEFAULT_MAX_NET_FANOUT
): boolean {
  const bare = name.toLowerCase().split("/").pop() ?? "";
  // Split on separators so "VINT" and "VREFBUF_OUT" are not mistaken for rails
  // by a naive prefix match, while "VCC_3V3" and "+3V3" still are.
  const tokens = bare.split(/[^a-z0-9]+/).filter(Boolean);
  if (tokens.some((t) => POWER_NET_PATTERNS.has(t) || VOLTAGE_RAIL_RE.test(t))) {
    return true;
  }
  return terminalCount > maxFanout;
}

/** How an endpoint is written wherever it is shown: `U1.AVDD`, `C3.1`. */
export function terminalLabel(endpoint: SchematicPin): string {
  return `${endpoint.ref ?? endpoint.part_id}.${endpoint.pin}`;
}

/** Board refs a net touches, in endpoint order, without duplicates. */
export function netRefs(net: SchematicNet): string[] {
  const seen: string[] = [];
  for (const endpoint of net.endpoints ?? []) {
    const ref = endpoint.ref ?? endpoint.part_id;
    if (!seen.includes(ref)) seen.push(ref);
  }
  return seen;
}

type SortKey = "engine" | "name" | "fanout" | "power";

const SORT_LABELS: Record<SortKey, string> = {
  engine: "Engine order",
  name: "Name",
  fanout: "Most terminals",
  power: "Power first",
};

export interface NetListProps {
  /** The response's `schematic` block. Undefined/null renders "not available". */
  schematic?: Schematic | null;
  /** Board refs to highlight, typically driven by the board view's selection. */
  selectedRefs?: string[];
  /** Net name to highlight, if a net is the current selection. */
  selectedNet?: string | null;
  onSelect?: SchematicSelect;
  className?: string;
}

export function NetList({
  schematic,
  selectedRefs = [],
  selectedNet = null,
  onSelect,
  className,
}: NetListProps) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("engine");

  const nets = schematic?.nets;

  const rows = useMemo(() => {
    const all = (nets ?? []).map((net, index) => {
      const endpoints = net.endpoints ?? [];
      return {
        net,
        index,
        endpoints,
        power: isPowerNet(net.name, endpoints.length),
        refs: netRefs(net),
        // Matching the pin-level label means a search for "U1.AVDD" or ".1"
        // finds the terminal, not just the part.
        haystack: [net.name, ...endpoints.map(terminalLabel)]
          .join(" ")
          .toLowerCase(),
      };
    });

    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? all.filter((row) => row.haystack.includes(needle))
      : all;

    // Every comparator falls back to the engine's own order, so the list is a
    // total order rather than whatever the sort happened to leave equal pairs in.
    const byIndex = (a: (typeof all)[number], b: (typeof all)[number]) =>
      a.index - b.index;
    const sorted = [...filtered];
    if (sort === "name") {
      sorted.sort((a, b) => a.net.name.localeCompare(b.net.name) || byIndex(a, b));
    } else if (sort === "fanout") {
      sorted.sort(
        (a, b) => b.endpoints.length - a.endpoints.length || byIndex(a, b)
      );
    } else if (sort === "power") {
      sorted.sort(
        (a, b) => Number(b.power) - Number(a.power) || byIndex(a, b)
      );
    }
    return { rows: sorted, total: all.length };
  }, [nets, query, sort]);

  if (!nets || nets.length === 0) {
    return (
      <EmptyComponent className={cn("border", className)}>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Zap />
          </EmptyMedia>
          <EmptyTitle>No net list available</EmptyTitle>
          <EmptyDescription>
            {nets
              ? "This run's schematic block carried no nets, so there is no connectivity to show."
              : "This run returned no schematic block. Connectivity was not reported — it is unknown, not empty."}
          </EmptyDescription>
        </EmptyHeader>
      </EmptyComponent>
    );
  }

  const powerCount = rows.rows.filter((row) => row.power).length;

  return (
    <div className={cn("flex min-h-0 flex-col gap-2", className)}>
      <div className="flex items-center gap-2">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter nets, parts or pins…"
          className="h-8"
          data-testid="netlist-filter"
        />
        <Select value={sort} onValueChange={(value) => setSort(value as SortKey)}>
          <SelectTrigger className="h-8 w-40" data-testid="netlist-sort">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => (
              <SelectItem key={key} value={key}>
                {SORT_LABELS[key]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <p
        className="text-muted-foreground text-xs"
        data-testid="netlist-summary"
      >
        {rows.rows.length} of {rows.total} nets
        {powerCount > 0 ? ` · ${powerCount} marked as power/ground` : ""}
      </p>

      <ScrollArea className="min-h-0 flex-1">
        <ul className="flex flex-col gap-1 pr-2">
          {rows.rows.map((row) => {
            const active =
              selectedNet === row.net.name ||
              row.refs.some((ref) => selectedRefs.includes(ref));
            return (
              <li key={row.net.name} data-testid="netlist-net" data-net={row.net.name}>
                {/* A div, not a button: the terminal chips inside are clickable
                    too, and interactive elements may not nest. */}
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() =>
                    onSelect?.({ refs: row.refs, net: row.net.name })
                  }
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    onSelect?.({ refs: row.refs, net: row.net.name });
                  }}
                  aria-pressed={active}
                  className={cn(
                    "w-full rounded-md border px-2 py-1.5 text-left transition-colors",
                    active
                      ? "border-ring bg-accent"
                      : "border-transparent hover:bg-muted"
                  )}
                >
                  <span className="flex items-center gap-2">
                    <span className="truncate font-mono text-sm">
                      {row.net.name}
                    </span>
                    {row.power && (
                      <Badge variant="secondary" className="gap-1">
                        <Zap />
                        power
                      </Badge>
                    )}
                    <span className="text-muted-foreground ml-auto text-xs">
                      {row.endpoints.length}
                    </span>
                  </span>
                  <span className="mt-1 flex flex-wrap gap-1">
                    {row.endpoints.map((endpoint) => {
                      const ref = endpoint.ref ?? endpoint.part_id;
                      return (
                        <span
                          key={`${endpoint.part_id}.${endpoint.pin}`}
                          data-testid="netlist-terminal"
                          data-ref={ref}
                          data-pin={endpoint.pin}
                          role="button"
                          tabIndex={0}
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelect?.({ refs: [ref], net: null });
                          }}
                          onKeyDown={(event) => {
                            if (event.key !== "Enter" && event.key !== " ") return;
                            event.preventDefault();
                            event.stopPropagation();
                            onSelect?.({ refs: [ref], net: null });
                          }}
                          className={cn(
                            "rounded border px-1.5 py-0.5 font-mono text-[11px]",
                            selectedRefs.includes(ref)
                              ? "border-ring bg-background"
                              : "border-border bg-muted/60 hover:bg-background"
                          )}
                        >
                          {terminalLabel(endpoint)}
                          {endpoint.number && endpoint.number !== endpoint.pin && (
                            <span className="text-muted-foreground">
                              {" "}
                              (pin {endpoint.number})
                            </span>
                          )}
                        </span>
                      );
                    })}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      </ScrollArea>
    </div>
  );
}
