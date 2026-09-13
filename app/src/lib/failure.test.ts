// The two failures the strip owns. The test that matters here is not that a
// banner appears — it is that the banner names the address, measures how long
// ago the engine last answered, and carries the literal line that fixes it.

import { describe, expect, it } from "vitest";

import {
  callFailureNotice,
  engineDownNotice,
  enginePort,
  formatAgo,
  missingKeyNotice,
  startCommand,
} from "./failure";

describe("formatAgo", () => {
  it("is m:ss", () => {
    expect(formatAgo(42_000)).toBe("0:42");
    expect(formatAgo(725_000)).toBe("12:05");
    expect(formatAgo(0)).toBe("0:00");
  });

  it("never counts backwards or reports a non-number", () => {
    expect(formatAgo(-5000)).toBe("0:00");
    expect(formatAgo(Number.NaN)).toBe("0:00");
  });
});

describe("the command that starts the engine", () => {
  it("names the port this app is actually looking at", () => {
    expect(enginePort("http://127.0.0.1:8081")).toBe("8081");
    expect(enginePort("http://localhost:9000/")).toBe("9000");
    expect(startCommand("http://127.0.0.1:8081")).toBe(
      "PORT=8081 python -m service.app"
    );
    expect(startCommand("http://127.0.0.1:9099")).toBe(
      "PORT=9099 python -m service.app"
    );
  });

  it("falls back to the documented port rather than to nothing", () => {
    expect(startCommand("not a url")).toBe("PORT=8081 python -m service.app");
  });
});

describe("engineDownNotice", () => {
  it("names the address, how long ago it was seen, and the command", () => {
    const notice = engineDownNotice("http://127.0.0.1:8081", 1_000_000 - 42_000, 1_000_000);
    expect(notice.kind).toBe("offline");
    expect(notice.title).toContain("http://127.0.0.1:8081");
    expect(notice.detail).toContain("Last seen 0:42 ago");
    expect(notice.command).toBe("PORT=8081 python -m service.app");
    expect(notice.retryLabel).toBe("Retry");
  });

  it("says it has never reached the engine rather than inventing an interval", () => {
    const notice = engineDownNotice("http://127.0.0.1:8081", null, 1_000_000);
    expect(notice.detail).toContain("have not reached it since this window opened");
    expect(notice.detail).not.toMatch(/\d:\d\d/);
  });

  it("quotes the transport's own words when it had any", () => {
    const notice = engineDownNotice(
      "http://127.0.0.1:8081",
      null,
      1,
      "Connection refused"
    );
    expect(notice.detail).toContain("It said: Connection refused.");
  });

  it("speaks in the first person and does not shout", () => {
    const notice = engineDownNotice("http://127.0.0.1:8081", null, 1);
    expect(notice.title.startsWith("I cannot")).toBe(true);
    expect(`${notice.title} ${notice.detail}`).not.toContain("!");
    expect(`${notice.title} ${notice.detail}`.toLowerCase()).not.toMatch(
      /assistant|helper/
    );
  });
});

describe("missingKeyNotice", () => {
  it("carries the export line and says nothing was spent", () => {
    const notice = missingKeyNotice("http://127.0.0.1:8081", "GOOGLE_API_KEY is not set");
    expect(notice.kind).toBe("setup");
    expect(notice.command).toBe(
      "export GOOGLE_API_KEY=… && PORT=8081 python -m service.app"
    );
    expect(notice.detail).toContain("It said: GOOGLE_API_KEY is not set.");
    expect(notice.detail).toContain("Nothing was spent on this attempt.");
    // Nothing to press: the fix is in a terminal, not in this app.
    expect(notice.retryLabel).toBeNull();
  });
});

describe("callFailureNotice", () => {
  it("maps a setup failure to the key banner", () => {
    const notice = callFailureNotice(
      { kind: "setup", message: "the engine has no GOOGLE_API_KEY" },
      "http://127.0.0.1:8081"
    );
    expect(notice?.kind).toBe("setup");
    expect(notice?.command).toContain("export GOOGLE_API_KEY=…");
  });

  it("maps an offline failure to the unreachable banner, with the last sighting", () => {
    const notice = callFailureNotice(
      { kind: "offline", detail: "error sending request" },
      "http://127.0.0.1:8081",
      1_000_000 - 90_000,
      1_000_000
    );
    expect(notice?.kind).toBe("offline");
    expect(notice?.detail).toContain("Last seen 1:30 ago");
  });

  it("leaves every other failure to the panel that owns the run", () => {
    for (const kind of ["request", "server", "timeout", "upstream", "auth", "cancelled"]) {
      expect(callFailureNotice({ kind }, "http://127.0.0.1:8081")).toBeNull();
    }
    expect(callFailureNotice(null, "http://127.0.0.1:8081")).toBeNull();
  });
});
