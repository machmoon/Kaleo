import type { SkinId } from "@/lib/overlay-skin";

export type StripHighlight = "field" | "mic" | "run" | null;

interface StripPreviewProps {
  skin: SkinId;
  /** `sm` is the 200×36 picker tile; `lg` the 400×72 tour/heading version. */
  size?: "sm" | "lg";
  /** Which control to draw a ring around, for the tour. */
  highlight?: StripHighlight;
  className?: string;
}

/**
 * A miniature of the strip in the *current* tokens, so it follows the theme
 * card live — pick Dark and every strip preview goes dark with it. Four
 * shapes, one per skin, each honest about what that skin actually shows.
 */
export const StripPreview = ({ skin, size = "sm", highlight = null, className }: StripPreviewProps) => {
  const scale = size === "lg" ? 2 : 1;
  const w = 200 * scale;
  const h = 36 * scale;
  const ring = (id: StripHighlight) =>
    highlight === id ? { boxShadow: "0 0 0 2px var(--foreground)" } : undefined;
  const dot = { width: 6 * scale, height: 6 * scale };

  return (
    <div
      data-testid="strip-preview"
      data-skin={skin}
      data-size={size}
      aria-hidden="true"
      className={`relative flex items-center gap-2 rounded-full border bg-card text-card-foreground shadow-sm ${className ?? ""}`}
      style={{ width: w, height: h, padding: `0 ${10 * scale}px` }}
    >
      {skin === "orb" ? (
        <span
          data-part="orb"
          className="mx-auto rounded-full bg-foreground/80"
          style={{ width: 16 * scale, height: 16 * scale }}
        />
      ) : skin === "terminal" ? (
        <>
          <span className="font-mono text-muted-foreground" style={{ fontSize: 9 * scale }}>
            $
          </span>
          <span
            data-part="field"
            className="h-[3px] flex-1 rounded-full bg-muted-foreground/40"
            style={{ ...ring("field"), height: 3 * scale }}
          />
          <span className="block bg-foreground" style={{ width: 4 * scale, height: 10 * scale }} />
        </>
      ) : (
        <>
          {skin === "spotlight" ? (
            <span
              className="rounded-full border border-muted-foreground/60"
              style={{ width: 8 * scale, height: 8 * scale }}
            />
          ) : (
            <span data-part="dot" className="rounded-full bg-emerald-500" style={dot} />
          )}
          <span
            data-part="field"
            className="flex-1 rounded-full bg-muted"
            style={{ height: 14 * scale, ...ring("field") }}
          />
          {skin === "plain" ? (
            <>
              <span
                data-part="mic"
                className="rounded-full bg-muted-foreground/50"
                style={{ width: 10 * scale, height: 10 * scale, ...ring("mic") }}
              />
              <span
                data-part="run"
                className="rounded-full bg-foreground"
                style={{ width: 14 * scale, height: 14 * scale, ...ring("run") }}
              />
            </>
          ) : null}
        </>
      )}
    </div>
  );
};
