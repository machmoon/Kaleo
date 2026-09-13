import { useRef } from "react";
import { useTheme } from "@/contexts";
import { ThemeThumbnail, type ThemeChoice } from "./ThemeThumbnail";
import { rovingKeyDown } from "./roving";

const CHOICES: readonly { id: ThemeChoice; label: string }[] = [
  { id: "system", label: "Auto" },
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
];

/**
 * The selection ring is macOS blue, hard-coded, on this radiogroup only.
 * Like the wallpaper hues it depicts the OS — the app's `--ring` stays
 * neutral everywhere else.
 */
export const THEME_RING = "oklch(0.62 0.19 255)";

/**
 * Apple's Appearance picker: three thumbnails, one radiogroup, roving
 * tabindex. Applies live through `useTheme().setTheme`, which writes the
 * legacy localStorage key the other webview already follows.
 */
export const ThemeCardPicker = ({ className }: { className?: string }) => {
  const { theme, setTheme } = useTheme();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const ids = CHOICES.map((c) => c.id);
  const onKeyDown = rovingKeyDown(ids, theme, setTheme, (i) => refs.current[i]?.focus());

  return (
    <div
      role="radiogroup"
      aria-label="Appearance"
      className={`flex items-start justify-center gap-6 ${className ?? ""}`}
      onKeyDown={onKeyDown}
      data-testid="theme-picker"
    >
      {CHOICES.map((choice, i) => {
        const selected = theme === choice.id;
        return (
          <button
            key={choice.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => setTheme(choice.id)}
            data-testid="theme-card"
            data-choice={choice.id}
            className="group flex cursor-pointer flex-col items-center gap-2 rounded-lg outline-none"
          >
            <span
              className="rounded-[10px] p-[2px]"
              style={{
                boxShadow: selected
                  ? `0 0 0 2px var(--background), 0 0 0 4px ${THEME_RING}`
                  : "0 0 0 1px var(--border)",
              }}
            >
              <ThemeThumbnail choice={choice.id} />
            </span>
            <span className={`text-[13px] ${selected ? "font-medium" : "text-muted-foreground"}`}>
              {choice.label}
            </span>
          </button>
        );
      })}
    </div>
  );
};
