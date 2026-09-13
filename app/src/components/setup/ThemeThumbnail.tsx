import type { CSSProperties } from "react";

export type ThemeChoice = "system" | "light" | "dark";

/**
 * A 160×110 picture of macOS in one theme, drawn in CSS.
 *
 * Colours are hard-coded on purpose: this depicts an operating system, not
 * the app's tokens. A thumbnail that followed `--background` would show the
 * theme you already have on every card.
 */
const WALLPAPER = {
  light: "linear-gradient(135deg, oklch(0.85 0.08 250), oklch(0.9 0.06 330))",
  dark: "linear-gradient(135deg, oklch(0.35 0.08 250), oklch(0.3 0.06 330))",
} as const;

const PALETTE = {
  light: { bar: "rgba(255,255,255,0.72)", window: "#ffffff", chrome: "#ececec", line: "#d0d0d0" },
  dark: { bar: "rgba(30,30,30,0.72)", window: "#1e1e1e", chrome: "#2b2b2b", line: "#4a4a4a" },
} as const;

const TRAFFIC = ["#ff5f57", "#febc2e", "#28c840"] as const;

const Scene = ({ mode, style }: { mode: "light" | "dark"; style?: CSSProperties }) => {
  const p = PALETTE[mode];
  return (
    <div
      aria-hidden="true"
      data-scene={mode}
      style={{
        position: "absolute",
        inset: 0,
        background: WALLPAPER[mode],
        ...style,
      }}
    >
      {/* 8 px menu bar */}
      <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 8, background: p.bar }} />
      {/* 96×60 window */}
      <div
        style={{
          position: "absolute",
          left: 32,
          top: 30,
          width: 96,
          height: 60,
          borderRadius: 5,
          background: p.window,
          boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: 12,
            background: p.chrome,
            display: "flex",
            alignItems: "center",
            gap: 3,
            paddingLeft: 5,
          }}
        >
          {TRAFFIC.map((c) => (
            <span key={c} style={{ width: 5, height: 5, borderRadius: 999, background: c }} />
          ))}
        </div>
        <div style={{ margin: "8px 8px 0", height: 4, width: 56, borderRadius: 2, background: p.line }} />
        <div style={{ margin: "5px 8px 0", height: 4, width: 40, borderRadius: 2, background: p.line }} />
      </div>
    </div>
  );
};

/**
 * Auto is the dark scene with the light scene clipped over its left half:
 * the same split Apple's own Appearance picker draws.
 */
export const ThemeThumbnail = ({ choice }: { choice: ThemeChoice }) => (
  <div
    data-testid="theme-thumbnail"
    data-choice={choice}
    style={{ position: "relative", width: 160, height: 110, borderRadius: 8, overflow: "hidden" }}
  >
    {choice === "light" ? <Scene mode="light" /> : null}
    {choice === "dark" ? <Scene mode="dark" /> : null}
    {choice === "system" ? (
      <>
        <Scene mode="dark" />
        <Scene mode="light" style={{ clipPath: "inset(0 50% 0 0)" }} />
      </>
    ) : null}
  </div>
);
