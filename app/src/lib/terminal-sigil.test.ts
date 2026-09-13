import { describe, expect, it } from "vitest";
import { LEGEND, route } from "./terminal-sigil";

describe("terminal sigil routing", () => {
  it("sends an ordinary command to the shell", () => {
    expect(route("ls -la").destination).toBe("shell");
    expect(route("git status").destination).toBe("shell");
    expect(route("./configure").destination).toBe("shell");
  });

  it("sends a capitalised line to Ada", () => {
    const routed = route("Why did that build fail?");
    expect(routed.destination).toBe("ada");
    expect(routed.text).toBe("Why did that build fail?");
  });

  it("treats ! as an agent request and strips the sigil", () => {
    const routed = route("!fix the footprint on U3");
    expect(routed.destination).toBe("agent");
    expect(routed.text).toBe("fix the footprint on U3");
  });

  it("leaves a bare ! to the shell, where it is history expansion", () => {
    // `!!` and `!42` are real shell syntax. Eating them would break a reflex
    // people have had for forty years.
    expect(route("!").destination).toBe("shell");
    expect(route("!!").destination).toBe("agent");
  });

  it("lets a leading space force the shell for a capitalised program", () => {
    // The failure that would make someone turn this off: `Rscript` is a real
    // command, and silently asking a model about it instead of running it is
    // both wrong and expensive.
    const routed = route(" Rscript analyse.R");
    expect(routed.destination).toBe("shell");
    expect(routed.text).toBe("Rscript analyse.R");
    expect(route("Rscript analyse.R").destination).toBe("ada");
  });

  it("routes nothing at all while a full-screen program is running", () => {
    // Inside vim, `:wq` is not a shell command and `Quit` is not a question.
    for (const line of [":wq", "Quit", "!q", "dd"]) {
      expect(route(line, { buffer: "alternate" }).destination).toBe("shell");
      expect(route(line, { buffer: "alternate" }).text).toBe(line);
    }
  });

  it("is a plain terminal when Ada routing is switched off", () => {
    expect(route("Why is this failing?", { adaEnabled: false }).destination).toBe("shell");
  });

  it("passes a bare Enter through so the prompt redraws", () => {
    expect(route("").destination).toBe("shell");
    expect(route("   ").destination).toBe("shell");
  });

  it("always explains itself, so the hint under the bar is never empty", () => {
    for (const line of ["ls", "Why", "!go", " X", "", ":wq"]) {
      expect(route(line).why.length).toBeGreaterThan(0);
    }
  });

  it("never invents text that the user did not type", () => {
    // The only edit this module may make is removing a leading sigil.
    for (const line of ["ls -la", "Why not?", "!do it", " Rscript x.R"]) {
      expect(line).toContain(route(line).text);
    }
  });

  it("has a legend that names both sigils", () => {
    expect(LEGEND.join(" ")).toMatch(/Capital/);
    expect(LEGEND.join(" ")).toMatch(/!/);
  });
});
