// @vitest-environment jsdom
//
// The integrations page: it asks the engine once what exists, groups what
// comes back, keeps unavailable and unconfigured readably different, states
// unverified where the engine states it, and never turns a failed read into an
// empty page.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

const fetchIntegrations = vi.fn();
const runIntegrationAction = vi.fn();
vi.mock("@/lib/silkscreen/integrations", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/silkscreen/integrations")
  >("@/lib/silkscreen/integrations");
  return {
    ...actual,
    fetchIntegrations: (...args: unknown[]) => fetchIntegrations(...args),
    runIntegrationAction: (...args: unknown[]) => runIntegrationAction(...args),
  };
});

import { MemoryRouter } from "react-router-dom";

import type { Integration } from "@/lib/silkscreen/integrations";
import Integrations from "./index";

/** `Header` calls `useNavigate`, so the page needs a router around it. */
const renderPage = () =>
  render(
    <MemoryRouter>
      <Integrations />
    </MemoryRouter>
  );

function integration(partial: Partial<Integration> & { id: string }): Integration {
  return {
    name: partial.id,
    kind: "delivery",
    state: "ready",
    summary: `what ${partial.id} does`,
    detail: "",
    settings: [],
    hints: [],
    actions: [],
    docs: "",
    unverified: false,
    ...partial,
  };
}

const GOOGLE = integration({
  id: "google",
  name: "Google Workspace",
  kind: "delivery",
  state: "partial",
  summary: "Posts a card, emails the board, books the review.",
  detail: "Chat webhook set; OAuth client missing.",
  settings: [
    { key: "GOOGLEAPPS_CHAT_WEBHOOK", required: true, set: true, shown: "…4f2a", note: "" },
    { key: "GOOGLEAPPS_CLIENT_ID", required: true, set: false, shown: "", note: "" },
  ],
  hints: [
    "Set GOOGLEAPPS_CLIENT_ID in the service's environment (the service does not read .env).",
  ],
  actions: [
    { id: "google_sign_in", label: "Sign in with Google", method: "POST", path: "/deliver/auth/start" },
  ],
  docs: "docs/googleapps.md",
});

const SLACK = integration({
  id: "slack",
  name: "Slack",
  kind: "delivery",
  state: "unconfigured",
  summary: "Posts runs into a thread.",
  hints: ["Set SLACK_BOT_TOKEN in the service's environment."],
});

const ZOOM = integration({
  id: "zoom",
  name: "Zoom",
  kind: "meeting",
  state: "unavailable",
  summary: "Joins a call and builds what was asked for.",
  hints: ["The zoombot package is not installed in this engine."],
});

const MEET = integration({
  id: "meet",
  name: "Google Meet",
  kind: "meeting",
  state: "ready",
  summary: "Reads a finished meeting's transcript.",
  unverified: true,
});

const CAD = integration({
  id: "cad",
  name: "Enclosure kernel",
  kind: "design",
  state: "unavailable",
  summary: "Real B-rep enclosures.",
  hints: ['Install the "cad" extra: pip install -e ".[cad]".'],
});

const KICAD = integration({
  id: "kicad",
  name: "KiCad",
  kind: "fabrication",
  state: "ready",
  summary: "Opens the generated project.",
});

const ROSTER = [GOOGLE, SLACK, ZOOM, MEET, CAD, KICAD];

const card = (id: string): HTMLElement => {
  const found = screen
    .getAllByTestId("integration-card")
    .find((element) => element.getAttribute("data-integration") === id);
  if (!found) throw new Error(`no card for ${id}`);
  return found;
};

describe("Integrations page", () => {
  beforeEach(() => {
    fetchIntegrations.mockReset();
    runIntegrationAction.mockReset();
    fetchIntegrations.mockResolvedValue(ROSTER);
    runIntegrationAction.mockResolvedValue({});
  });
  afterEach(cleanup);

  it("renders every integration, grouped by kind in the frozen order", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    const kinds = screen
      .getAllByTestId("integration-group")
      .map((group) => group.getAttribute("data-kind"));
    expect(kinds).toEqual(["delivery", "meeting", "design", "fabrication"]);

    // Each card sits under its own kind's group, not merely somewhere on page.
    const delivery = screen
      .getAllByTestId("integration-group")
      .find((group) => group.getAttribute("data-kind") === "delivery")!;
    expect(delivery.contains(card("google"))).toBe(true);
    expect(delivery.contains(card("slack"))).toBe(true);
  });

  it("shows an unavailable integration rather than hiding it, and says something different than for an unconfigured one", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    const unavailable = card("zoom");
    const unconfigured = card("slack");
    expect(unavailable).toBeTruthy();
    expect(unavailable.getAttribute("data-state")).toBe("unavailable");
    expect(unconfigured.getAttribute("data-state")).toBe("unconfigured");

    const noteOf = (element: HTMLElement) =>
      element.querySelector('[data-testid="integration-state-note"]')?.textContent ?? "";
    // The distinction is the point of the panel: one cannot be fixed with an
    // environment variable and the other can.
    expect(noteOf(unavailable)).not.toEqual(noteOf(unconfigured));
    expect(noteOf(unavailable)).toMatch(/not installed/i);
    expect(noteOf(unconfigured)).toMatch(/nothing is configured/i);

    // And the engine's own hint is reproduced verbatim on both.
    expect(unavailable.textContent).toContain(
      "The zoombot package is not installed in this engine."
    );
    expect(unconfigured.textContent).toContain(
      "Set SLACK_BOT_TOKEN in the service's environment."
    );
  });

  it("marks an unverified integration visibly", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    const markers = screen.getAllByTestId("integration-unverified");
    expect(markers).toHaveLength(1);
    expect(markers[0].getAttribute("data-integration")).toBe("meet");
    expect(markers[0].textContent).toMatch(/never run live/i);
    // Ready and unverified are not in conflict: one is about configuration,
    // the other about whether it has ever actually run.
    expect(card("meet").getAttribute("data-state")).toBe("ready");
  });

  it("shows a setting as set or not set, and never a value", async () => {
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    const settings = card("google").querySelectorAll('[data-testid="integration-setting"]');
    expect(settings).toHaveLength(2);
    expect(settings[0].getAttribute("data-key")).toBe("GOOGLEAPPS_CHAT_WEBHOOK");
    expect(settings[0].getAttribute("data-set")).toBe("yes");
    expect(settings[1].getAttribute("data-set")).toBe("no");
    expect(settings[1].textContent).toMatch(/not set \(required\)/);
  });

  it("shows the failure rather than an empty page when the read fails", async () => {
    fetchIntegrations.mockRejectedValue(new Error("connection refused"));
    renderPage();

    await waitFor(() => expect(screen.getByTestId("integrations-error")).toBeTruthy());
    expect(screen.getByTestId("integrations-error").textContent).toContain(
      "connection refused"
    );
    // An empty list would read as "you have no integrations", which is a lie.
    expect(screen.queryAllByTestId("integration-card")).toHaveLength(0);
    expect(screen.queryByTestId("integrations-empty")).toBeNull();
  });

  it("refetches when refresh is pressed", async () => {
    renderPage();
    await waitFor(() => expect(fetchIntegrations).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByTestId("integrations-refresh"));
    await waitFor(() => expect(fetchIntegrations).toHaveBeenCalledTimes(2));
  });

  it("fires an action once and disables the buttons while it is in flight", async () => {
    let release: (value: unknown) => void = () => {};
    runIntegrationAction.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      })
    );

    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    const button = screen.getByTestId("integration-action");
    fireEvent.click(button);
    // A second click while the first call is open would be a second real call.
    fireEvent.click(button);
    expect(runIntegrationAction).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByTestId("integration-action")).toHaveProperty("disabled", true)
    );

    release({});
    await waitFor(() =>
      expect(screen.getByTestId("integration-action")).toHaveProperty("disabled", false)
    );
    // The action changed the engine's state, so the page rereads it.
    expect(fetchIntegrations).toHaveBeenCalledTimes(2);
  });

  it("reports a failed action on that card instead of swallowing it", async () => {
    runIntegrationAction.mockRejectedValue(new Error("consent window closed"));
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("integration-card")).toHaveLength(6));

    fireEvent.click(screen.getByTestId("integration-action"));
    await waitFor(() => expect(screen.getByTestId("integration-failure")).toBeTruthy());
    const failure = screen.getByTestId("integration-failure");
    expect(failure.getAttribute("data-integration")).toBe("google");
    expect(failure.textContent).toContain("consent window closed");
  });
});
