// The ⌘K vocabulary: what it offers, what it refuses to offer, and the one
// property that makes the menu trustworthy — every item that arms is a
// command `interpretCommand` actually accepts.

import { describe, expect, it } from "vitest";

import { basename, commandItems, filterCommands } from "./commands";
import type { CommandContext, CommandItem } from "./commands";
import { COMMAND_VOCABULARY, interpretCommand, phrasesFor } from "./silkscreen/steps";
import type { StepName, StepResponse } from "./silkscreen/types";

function response(step: StepName, next: StepName[], files: Record<string, string> = {}): StepResponse {
  return {
    session: "s1",
    step,
    stage: "placed",
    intent: "a 3.3V LDO board",
    files,
    next,
    shown_in_kicad: true,
    events: [],
    duration_s: 1,
  } as StepResponse;
}

function context(overrides: Partial<CommandContext> = {}): CommandContext {
  return {
    status: "waiting",
    available: ["route"],
    background: [],
    history: [response("place", ["route"], { placed_board: "/tmp/ldo/board.placed.kicad_pcb" })],
    ...overrides,
  };
}

const byId = (items: CommandItem[], id: string) => items.find((item) => item.id === id);

describe("commandItems — what may run now", () => {
  it("offers one Next item per available step, priced, and none while a step is in flight", () => {
    const waiting = commandItems(context({ available: ["route", "case"] }));
    expect(waiting.filter((i) => i.group === "Next").map((i) => i.label)).toEqual([
      "Route copper",
      "Design the case",
    ]);
    expect(byId(waiting, "step:route")?.cost).toBe("1 call");
    expect(byId(waiting, "step:route")?.paid).toBe(true);

    const running = commandItems(context({ status: "running", available: ["route"] }));
    expect(running.some((i) => i.group === "Next")).toBe(false);
  });

  it("a stage the engine already started costs 0 calls and says why", () => {
    const items = commandItems(context({ available: ["case"], background: ["case"] }));
    const item = byId(items, "step:case");
    expect(item?.cost).toBe("0 calls");
    expect(item?.paid).toBe(false);
    expect(item?.detail).toBe("already started, collects the result");
  });

  it("an impossible command is absent, not offered: no Reveal without a file, no Open without a board", () => {
    const empty = commandItems(context({ history: [response("propose", ["place"])] }));
    expect(byId(empty, "reveal")).toBeUndefined();
    expect(byId(empty, "open")).toBeUndefined();

    const routed = commandItems(
      context({ history: [response("route", ["review"], { board: "/tmp/ldo/ldo.kicad_pcb" })] })
    );
    expect(byId(routed, "reveal")?.label).toBe("Reveal ldo.kicad_pcb");
    expect(byId(routed, "open")?.label).toBe("Open the board file");
    // The app hands the file to the OS; it never claims which program answers.
    expect(byId(routed, "open")?.detail).not.toMatch(/pcbnew/i);
  });

  it("Cancel exists only while a step is in flight, Stop here only when one is not", () => {
    expect(byId(commandItems(context({ status: "running" })), "cancel")?.key).toBe("esc");
    expect(byId(commandItems(context({ status: "running" })), "stop")).toBeUndefined();
    expect(byId(commandItems(context({ status: "waiting" })), "stop")?.key).toBe("esc");
    expect(byId(commandItems(context({ status: "waiting" })), "cancel")).toBeUndefined();
    expect(byId(commandItems(context({ status: "idle", history: [] })), "stop")).toBeUndefined();
  });

  it("Start over is destructive, counts the paid stages it drops, and always arms", () => {
    const items = commandItems(
      context({ history: [response("propose", ["place"]), response("place", ["route"])] })
    );
    const restart = byId(items, "restart");
    expect(restart?.danger).toBe(true);
    expect(restart?.arms).toBe(true);
    expect(restart?.detail).toBe("drops 2 paid stages, asks first");
    expect(restart?.label).toBe("Start over");
    // Nothing is offered to drop before anything has been spent.
    expect(byId(commandItems(context({ history: [], status: "idle" })), "restart")).toBeUndefined();
  });

  it("a replayed run offers nothing that spends and nothing that drops", () => {
    const items = commandItems(context({ replayed: true, available: ["route"] }));
    expect(items.some((i) => i.paid)).toBe(false);
    expect(items.some((i) => i.group === "Next")).toBe(false);
    expect(byId(items, "restart")).toBeUndefined();
  });

  it("the Replay group is absent until this build can actually open a recording", () => {
    expect(commandItems(context()).some((i) => i.group === "Replay")).toBe(false);
    expect(byId(commandItems(context({ canReplay: true })), "replay")?.group).toBe("Replay");
  });

  it("speaks in the first person and never calls itself an assistant", () => {
    const text = commandItems(context({ available: ["route", "case"], background: ["case"], canReplay: true }))
      .map((i) => `${i.label} ${i.detail ?? ""}`)
      .join(" ")
      .toLowerCase();
    expect(text).not.toMatch(/assistant|helper/);
  });
});

describe("the menu and the interpreter share one vocabulary", () => {
  it("every arming item round-trips through interpretCommand", () => {
    const items = commandItems(
      context({
        available: ["route", "sourcing", "order", "case"],
        background: ["case"],
        history: [response("place", ["route"], { placed_board: "/tmp/b.kicad_pcb" })],
      })
    );
    const arming = items.filter((item) => item.arms);
    expect(arming.length).toBeGreaterThan(0);
    for (const item of arming) {
      expect(item.phrases.length).toBeGreaterThan(0);
      for (const phrase of item.phrases) {
        const command = interpretCommand(phrase, ["route", "sourcing", "order", "case"]);
        if (item.step) expect(command).toEqual({ kind: "approve", step: item.step });
        else expect(command).toEqual({ kind: "restart" });
      }
    }
  });

  it("an item the interpreter has no words for claims none, and never arms", () => {
    const items = commandItems(
      context({ status: "running", history: [response("place", ["route"], { board: "/tmp/b.kicad_pcb" })] })
    );
    for (const item of items.filter((i) => !i.arms)) expect(item.phrases).toEqual([]);
  });

  it("the vocabulary covers every step plus the restart, and nothing else", () => {
    expect(COMMAND_VOCABULARY.map((entry) => entry.command.kind)).toEqual([
      "approve",
      "approve",
      "approve",
      "approve",
      "approve",
      "approve",
      "approve",
      "restart",
    ]);
    expect(phrasesFor({ kind: "approve", step: "route" })).toContain("copper");
    expect(phrasesFor({ kind: "restart" })).toContain("start over");
    // The kinds the vocabulary has no words for: nothing selects them by
    // being typed, so the menu says nothing about typing them.
    expect(phrasesFor({ kind: "none" })).toEqual([]);
    expect(phrasesFor({ kind: "request", text: "a buck converter" })).toEqual([]);
  });
});

describe("filterCommands", () => {
  const items = commandItems(
    context({
      available: ["route", "case"],
      history: [response("place", ["route", "case"], { placed_board: "/tmp/ldo/board.kicad_pcb" })],
    })
  );

  it("an empty query is everything, in the order the list was built", () => {
    expect(filterCommands(items, "").map((m) => m.item.id)).toEqual(items.map((i) => i.id));
    expect(filterCommands(items, "   ").length).toBe(items.length);
  });

  it("ranks a label prefix above a word inside it, and marks where it matched", () => {
    const hits = filterCommands(items, "ro");
    expect(hits[0].item.id).toBe("step:route");
    expect(hits[0].range).toEqual([0, 2]);
    const inner = filterCommands(items, "copper");
    expect(inner[0].item.id).toBe("step:route");
    // "Route copper" — the highlight sits on the second word, not at zero.
    expect(inner[0].range?.[0]).toBe("Route ".length);
  });

  it("finds an item by a spoken phrase the interpreter accepts, with no label highlight", () => {
    const hits = filterCommands(items, "enclosure");
    expect(hits[0].item.id).toBe("step:case");
    expect(hits[0].range).toBeNull();
  });

  it("returns nothing for a sentence it does not know, rather than guessing", () => {
    expect(filterCommands(items, "make it purple")).toEqual([]);
  });
});

describe("basename", () => {
  it("takes the last component of either separator, and survives a bare name", () => {
    expect(basename("/tmp/ldo/board.kicad_pcb")).toBe("board.kicad_pcb");
    expect(basename("C:\\runs\\ldo\\board.kicad_pcb")).toBe("board.kicad_pcb");
    expect(basename("board.kicad_pcb")).toBe("board.kicad_pcb");
  });
});
