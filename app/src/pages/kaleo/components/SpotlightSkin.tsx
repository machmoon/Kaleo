import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { EngineHealth } from "@/hooks";
import { filterCommands, type CommandItem } from "@/lib/commands";
import { cn } from "@/lib/utils";

/**
 * The overlay's Spotlight skin: one field, and a list only when there is one.
 *
 * The brief this exists for is subtraction — "i think a lot of the design
 * have a lot of clutter but ig i like the serach bar from apple", and "the
 * overlay isnt meant to have so much visual clutter". So at rest this is a
 * single bare `<input>` and literally nothing else: no toolbar, no chips, no
 * status strip, no submit button sitting there greyed out waiting to be
 * earned. Every other element in this file is conditional, and the condition
 * is always "there is now a reason for it".
 *
 * The shape is cmdk's (`pacocoursey/cmdk`) rather than a hand-rolled search
 * box, and `docs/overlay-skins.md` records what reading the real source
 * changed:
 *
 *  - cmdk's core renders ONE bare `Primitive.input` with `border: none;
 *    outline: none` and puts any border on the root. Kaleo's `PromptBar`
 *    wraps its field in `h-9 rounded-md border border-input/50 bg-muted/30`;
 *    that inner bordered box is the hand-rolled tell and it is absent here —
 *    the panel (the overlay `Card`) carries the radius, the field carries
 *    nothing.
 *  - `outline: none` is not cosmetic. sol passes `enableFocusRing={false}`
 *    for the same reason: without it macOS paints a blue focus ring *inside*
 *    a borderless panel, which is the one piece of chrome this skin cannot
 *    afford.
 *  - The real Spotlight blur is an NSVisualEffectView added *below* the
 *    webview (window-vibrancy), not a CSS `backdrop-filter`, which would only
 *    blur what this webview already painted and never the KiCad canvas
 *    behind the window. So there is no blur in this file at all — that half
 *    is a Rust change (`apply_vibrancy` in `src-tauri/src/lib.rs`), and
 *    faking it here would look like the feature while doing nothing.
 *  - cmdk's Linear preset sets `caret-color: #6e5ed2`. Copied literally that
 *    is a second hue on a bar that has to sit on both KiCad grounds, so the
 *    caret takes the app's own `primary` instead, and nothing here flips
 *    with the OS theme.
 *
 * The component is pure presentation: it holds the highlighted row and
 * nothing else. The query, the run, the command list and the engine all
 * belong to the page, so this skin can be dropped in beside `PromptBar`
 * without a second copy of any of that state.
 */

/** A row in the list, after the two sources have been flattened into one. */
interface SpotlightRow {
  id: string;
  label: string;
  detail: string | null;
  /** `1 call` / `0 calls`, or null for something that spends nothing. */
  cost: string | null;
  /** True when taking this row makes a paid engine call. */
  paid: boolean;
  danger: boolean;
  /** Where the query matched the label, for the one accent in the list. */
  range: [number, number] | null;
  /** The command this row runs, or null for the free-text board run. */
  item: CommandItem | null;
}

export interface SpotlightSkinProps {
  /** What is typed. Owned by the page, exactly as `PromptBar` has it. */
  value: string;
  onChange: (value: string) => void;
  /**
   * Start a board from the typed sentence. Reached only through the run row,
   * which only exists when a run is actually valid — see `canSpend`.
   */
  onSubmit: () => void;
  /**
   * The commands that may run right now — `commandItems(...)` from
   * `@/lib/commands`, unfiltered. Filtering happens here, the way cmdk does
   * it, so the page does not have to track the query twice.
   *
   * That list is already the "absent, not greyed" list: an impossible
   * command is not in it. Nothing here re-adds one in a disabled state.
   */
  results?: readonly CommandItem[];
  /** Take a command row. */
  onSelect?: (item: CommandItem) => void;
  /** Escape. The page hides the overlay; the text survives. */
  onClose?: () => void;
  /** A run is in flight, so nothing may spend and the field says so. */
  busy?: boolean;
  /** What is running, in one line. Shown only while `busy`. */
  busyLabel?: string;
  engine: EngineHealth;
  baseUrl: string;
  /** The overlay is hidden by the global shortcut; keep focus out of it. */
  hidden?: boolean;
  /** The price of the free-text run, on the control that spends it. */
  runCost?: string;
  /** The free-text run's own label. */
  runLabel?: string;
  placeholder?: string;
  /**
   * Take focus on mount. True in the overlay — the panel opens *for* typing —
   * and off in tests that want to prove focus is not stolen while hidden.
   */
  autoFocus?: boolean;
}

/**
 * The engine, as a shape rather than a hue — and only when it is worth saying.
 *
 * Three states, not two: before the first probe lands we do not know, and a
 * green dot then would be a guess while a red one would be an accusation. The
 * shape carries each state so it survives colour being stripped, which a
 * hue-only dot does not.
 *
 * The healthy state renders nothing at all. That is the whole skin's rule
 * applied to itself: "the engine is fine" is not a reason for an element, and
 * a permanent green dot beside a search field is exactly the clutter the
 * brief rejected. The marker appearing *is* the news.
 */
const EngineMark = ({ engine, baseUrl }: { engine: EngineHealth; baseUrl: string }) => {
  const unknown = engine.lastCheckedAt === null;
  if (!unknown && engine.ok) return null;

  const label = unknown
    ? `Checking ${baseUrl}…`
    : `Engine unreachable at ${baseUrl}${engine.detail ? ` — ${engine.detail}` : ""}`;

  return (
    <button
      type="button"
      className="flex size-4 shrink-0 items-center justify-center"
      title={`${label}. Click to re-check.`}
      aria-label={label}
      onClick={engine.recheck}
      data-testid="spotlight-engine"
      data-online={unknown ? "unknown" : "false"}
    >
      <span
        className={cn(
          "relative block size-2.5 rounded-full border border-muted-foreground/70",
          // Dashed hollow: nobody has asked the engine yet.
          unknown && "border-dashed",
          engine.checking && "animate-pulse",
        )}
      >
        {/* Slashed hollow: asked, and the answer was no. The slash is the
            state, not the colour — this reads the same in greyscale. */}
        {!unknown ? (
          <span className="absolute left-1/2 top-1/2 h-px w-3.5 -translate-x-1/2 -translate-y-1/2 rotate-45 bg-muted-foreground/70" />
        ) : null}
      </span>
    </button>
  );
};

/** The label with the matched span accented, which is cmdk's one flourish. */
const Label = ({ text, range }: { text: string; range: [number, number] | null }) => {
  if (!range) return <>{text}</>;
  return (
    <>
      {text.slice(0, range[0])}
      <span className="font-semibold">{text.slice(range[0], range[1])}</span>
      {text.slice(range[1])}
    </>
  );
};

export const SpotlightSkin = ({
  value,
  onChange,
  onSubmit,
  results = [],
  onSelect,
  onClose,
  busy = false,
  busyLabel = "Working…",
  engine,
  baseUrl,
  hidden = false,
  runCost = "1 call",
  runLabel = "Generate a board",
  placeholder = "What do you need built?",
  autoFocus = true,
}: SpotlightSkinProps) => {
  const field = useRef<HTMLInputElement | null>(null);
  const [selected, setSelected] = useState(0);
  const query = value.trim();

  /**
   * HOUSE RULE 6, and it overrides the Spotlight look: the control that
   * spends money is always visibly a paid control, and it is **absent, not
   * greyed**, when pressing it would not be valid. Every source project this
   * skin was read from keeps a persistent submit affordance and greys it;
   * a greyed control still reads as an offer, and the offer would be a lie
   * — nothing is typed, or the engine has never answered, or a paid run is
   * already in flight and a second press would buy a second one.
   *
   * So the run row is not disabled anywhere in this file. It either exists
   * and states its price, or it is not rendered.
   */
  const canSpend = query.length > 0 && engine.ok && !busy && !hidden;

  const rows = useMemo<SpotlightRow[]>(() => {
    // An empty field has no results by construction — this is the "one field
    // at rest" rule. `filterCommands("")` deliberately returns *everything*,
    // which is right for a ⌘K menu and wrong here: it would open a full list
    // under a field nobody has typed in yet.
    if (!query) return [];

    const runRow: SpotlightRow[] = canSpend
      ? [
          {
            id: "run",
            label: runLabel,
            detail: null,
            cost: runCost,
            paid: true,
            danger: false,
            range: null,
            item: null,
          },
        ]
      : [];

    const commands = filterCommands(results, query).map<SpotlightRow>((match) => ({
      id: match.item.id,
      label: match.item.label,
      detail: match.item.detail,
      cost: match.item.cost,
      paid: match.item.paid,
      danger: match.item.danger,
      range: match.range,
      item: match.item,
    }));

    return [...runRow, ...commands];
  }, [canSpend, query, results, runCost, runLabel]);

  // A new query is a new list, so the highlight goes back to the top rather
  // than staying on whatever row happens to hold that index now. Without this
  // Enter can take a row the user never looked at.
  useEffect(() => {
    setSelected(0);
  }, [query]);

  const active = rows.length === 0 ? -1 : Math.min(selected, rows.length - 1);

  useEffect(() => {
    // Never steal focus while the overlay is hidden by the global shortcut:
    // the panel is over KiCad, and typing that lands here instead of the
    // schematic is the one failure the shortcut exists to avoid.
    if (autoFocus && !hidden) field.current?.focus();
  }, [autoFocus, hidden]);

  const take = (row: SpotlightRow) => {
    if (row.item) onSelect?.(row.item);
    else onSubmit();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose?.();
      return;
    }
    if (event.key === "ArrowDown" && rows.length > 0) {
      event.preventDefault();
      // Clamped, not wrapped: on a list this short a wrap reads as the
      // highlight jumping rather than moving.
      setSelected((index) => Math.min(index + 1, rows.length - 1));
      return;
    }
    if (event.key === "ArrowUp" && rows.length > 0) {
      event.preventDefault();
      setSelected((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      // No row means no offer, and Enter does nothing at all. It must not
      // fall through to a run: an empty list is exactly the state where a
      // run is invalid.
      if (active >= 0) take(rows[active]);
    }
  };

  const listId = "spotlight-results";

  return (
    <div className="flex w-full min-w-0 flex-col" data-testid="spotlight-skin">
      <div className="flex w-full min-w-0 items-center gap-2 pr-3">
        <input
          ref={field}
          // One bare input, cmdk's core: no border, no ring, no ground of its
          // own. The panel around it owns the radius and the material.
          className="min-w-0 flex-1 border-none bg-transparent px-4 py-2 text-[15px] leading-normal caret-primary outline-none placeholder:text-muted-foreground focus:outline-none focus-visible:outline-none"
          placeholder={placeholder}
          value={value}
          // Deliberately never `disabled`, even while busy: a disabled field
          // is a greyed control, it drops the caret mid-sentence, and
          // `focusPromptIn` skips it — so cmd+shift+I would stop finding the
          // prompt exactly when a run is on screen.
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          data-testid="prompt-input"
          role="combobox"
          aria-expanded={rows.length > 0}
          aria-controls={listId}
          aria-activedescendant={active >= 0 ? `spotlight-row-${rows[active].id}` : undefined}
          aria-autocomplete="list"
          autoComplete="off"
          spellCheck={false}
        />
        <EngineMark engine={engine} baseUrl={baseUrl} />
      </div>

      {busy ? (
        // One dim line, and only while something is actually running. This is
        // a reason, not a status strip: it leaves with the run.
        <div
          className="px-4 pb-2 text-[11px] text-muted-foreground"
          role="status"
          data-testid="spotlight-busy"
        >
          {busyLabel}
        </div>
      ) : null}

      {rows.length > 0 ? (
        <ul
          id={listId}
          role="listbox"
          aria-label="Results"
          className="mt-1 flex max-h-56 flex-col overflow-y-auto border-t border-input/40 py-1"
          data-testid="spotlight-results"
        >
          {rows.map((row, index) => {
            const isActive = index === active;
            return (
              <li
                key={row.id}
                id={`spotlight-row-${row.id}`}
                role="option"
                aria-selected={isActive}
                data-testid={`spotlight-row-${row.id}`}
                data-paid={row.paid ? "true" : "false"}
                data-active={isActive ? "true" : "false"}
                className={cn(
                  "flex cursor-default items-center gap-2 px-4 py-1.5 text-[13px]",
                  isActive && "bg-muted/60",
                )}
                // Selection follows the pointer, as it does in Spotlight and
                // cmdk, so the keyboard and the mouse can never disagree
                // about which row Enter would take.
                onMouseMove={() => setSelected(index)}
                onClick={() => take(row)}
              >
                <span className="min-w-0 flex-1 truncate">
                  <Label text={row.label} range={row.range} />
                  {row.detail ? (
                    <span className="ml-2 text-muted-foreground">{row.detail}</span>
                  ) : null}
                </span>
                {row.cost ? (
                  <span
                    className={cn(
                      "shrink-0 rounded-sm px-1.5 py-0.5 text-[10px] leading-none",
                      // Rule 6's other half: a paid row is *visibly* paid. The
                      // filled treatment goes to the one row Enter would spend
                      // on, so a frame never shows two things that look like
                      // the money control.
                      row.paid && isActive && "bg-primary text-primary-foreground",
                      row.paid && !isActive && "border border-primary/60 text-foreground",
                      !row.paid && "text-muted-foreground",
                      row.danger && "border-destructive/70 text-destructive",
                    )}
                    data-testid={`spotlight-cost-${row.id}`}
                  >
                    {row.cost}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
};
