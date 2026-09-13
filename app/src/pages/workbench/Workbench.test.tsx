// @vitest-environment jsdom
//
// The far end of the hand-off. Everything else about a run being published is
// tested where it is written (`lib/silkscreen/bridge.test.ts`,
// `hooks/useStepRun.test.tsx`); what is asserted here is the only thing the
// engineer actually sees: the dashboard window opens ON the board rather than
// on its empty state, in the app's default (step-mode) configuration, where
// the window is created by the run it is meant to show and so misses the
// storage event that announced it.

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

vi.mock("@/hooks/useEngineHealth", () => ({
  useEngineHealth: () => ({
    baseUrl: "http://mock",
    ok: true,
    detail: "",
    checking: false,
    lastCheckedAt: null,
    recheck: () => {},
  }),
}));

import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";
import { RunProvider } from "@/contexts/run.context";
import Workbench from "./index";

/** What `useStepRun` writes to the bridge once `place` has landed. */
function publishPlacedBoard() {
  localStorage.setItem(
    KALEO_STORAGE_KEYS.LAST_RUN,
    JSON.stringify({
      id: "steps-s1",
      intent: "a toy car",
      at: 1_000,
      request: {
        intent: "a toy car",
        datasheets: {},
        time_limit_s: 20,
        review: false,
        ground: false,
        debug: false,
      },
      result: {
        kicad_pcb: "(kicad_pcb (version 20240108))",
        intent: "a toy car",
        status: "FEASIBLE",
        board_mm: [20, 15],
        placements: { board_mm: [20, 15], parts: [] },
      },
      progress: { stages: [], feed: [], status: "done" },
      startedAt: 0,
      finishedAt: 1_000,
      elapsedS: 1,
    })
  );
}

const mount = () =>
  render(
    <MemoryRouter initialEntries={["/workbench"]}>
      <RunProvider baseUrl="http://mock">
        <Workbench />
      </RunProvider>
    </MemoryRouter>
  );

beforeEach(() => localStorage.clear());

describe("the workbench as the bridge's consumer", () => {
  it("opens on the board a step run published before this window existed", () => {
    publishPlacedBoard();
    mount();
    expect(screen.queryByTestId("workbench-empty")).toBeNull();
    expect(screen.getByTestId("workbench-tab-board")).toBeTruthy();
    expect(screen.getByTestId("workbench-tab-artifacts")).toBeTruthy();
  });

  it("still says so plainly when nothing has been published", () => {
    mount();
    expect(screen.getByTestId("workbench-empty")).toBeTruthy();
  });
});
