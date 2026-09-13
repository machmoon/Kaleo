// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest";

import { focusPromptIn } from "./focus-prompt";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("focusPromptIn", () => {
  it("focuses the prompt field under the root and reports it", () => {
    document.body.innerHTML =
      '<div id="bar"><button>x</button><input data-testid="prompt-input" /></div>';
    const input = document.querySelector("input") as HTMLInputElement;
    expect(focusPromptIn(document.getElementById("bar"))).toBe(true);
    expect(document.activeElement).toBe(input);
  });

  it("reports false when the bar is folded and nothing is mounted", () => {
    document.body.innerHTML = '<div id="bar"><button>x</button></div>';
    expect(focusPromptIn(document.getElementById("bar"))).toBe(false);
    expect(focusPromptIn(null)).toBe(false);
  });

  it("leaves a disabled field alone, so a run in flight is not interrupted", () => {
    document.body.innerHTML = '<div id="bar"><input disabled /></div>';
    expect(focusPromptIn(document.getElementById("bar"))).toBe(false);
    expect(document.activeElement).toBe(document.body);
  });
});
