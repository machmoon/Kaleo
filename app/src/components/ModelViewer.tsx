import { useEffect, useRef, useState } from "react";
import { readFile } from "@tauri-apps/plugin-fs";
import { createRenderer, parseGlb } from "@/lib/gltf";
import type { Renderer } from "@/lib/gltf";
import { cn } from "@/lib/utils";

export interface ModelViewerProps {
  /** Absolute path of a `.glb`, the one the order step exported. */
  path: string;
  className?: string;
  /** Fired once per failed load with the sentence shown in the viewer. */
  onError?: (message: string) => void;
}

type Phase =
  | { kind: "loading" }
  | { kind: "ready"; meshes: number; warnings: string[] }
  | { kind: "lost"; meshes: number; warnings: string[] }
  | { kind: "error"; message: string };

function describe(caught: unknown): string {
  if (caught instanceof Error) return caught.message;
  return typeof caught === "string" ? caught : String(caught ?? "unknown error");
}

/**
 * The board in 3D, inside the overlay, from the GLB kicad-cli wrote.
 *
 * The canvas is always in the DOM (so the renderer has something to attach
 * to) and the status line over it says what is happening: reading, an error
 * sentence, or the mesh count and controls. When the webview has no WebGL2
 * the message says so — a blank canvas would look like an empty board. A
 * dropped WebGL context is reported and then waited out: the renderer
 * re-uploads when the browser hands the context back.
 */
export const ModelViewer = ({ path, className, onError }: ModelViewerProps) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<Renderer | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  // The latest callback, read at failure time, so a caller passing a fresh
  // arrow each render does not re-read the file every render.
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    let cancelled = false;
    setPhase({ kind: "loading" });

    const fail = (message: string) => {
      if (cancelled) return;
      setPhase({ kind: "error", message });
      onErrorRef.current?.(message);
    };

    (async () => {
      let bytes: Uint8Array;
      try {
        bytes = await readFile(path);
      } catch (caught) {
        fail(`Could not read ${path}: ${describe(caught)}`);
        return;
      }
      if (cancelled) return;
      try {
        const scene = parseGlb(bytes);
        if (cancelled) return;
        const canvas = canvasRef.current;
        if (!canvas) throw new Error("The viewer's canvas is not mounted.");
        const loaded = { meshes: scene.meshes.length, warnings: scene.warnings };
        const renderer = createRenderer(canvas, scene, {
          onContextLost: () => {
            if (!cancelled) setPhase({ kind: "lost", ...loaded });
          },
          onContextRestored: () => {
            if (!cancelled) setPhase({ kind: "ready", ...loaded });
          },
        });
        // The effect cleanup may have run while the file was being read or
        // parsed; a renderer created after that would never be disposed and
        // its listeners would fight the next path's renderer for the canvas.
        if (cancelled) {
          renderer.dispose();
          return;
        }
        rendererRef.current = renderer;
        setPhase({ kind: "ready", ...loaded });
      } catch (caught) {
        fail(describe(caught));
      }
    })();

    return () => {
      cancelled = true;
      rendererRef.current?.dispose();
      rendererRef.current = null;
    };
  }, [path]);

  const loaded = phase.kind === "ready" || phase.kind === "lost" ? phase : null;

  return (
    <div className={cn("relative overflow-hidden rounded-md border border-input/40 bg-[#1c1f24]", className)}>
      <canvas
        ref={canvasRef}
        className="block h-full w-full touch-none outline-none focus-visible:ring-1 focus-visible:ring-white/50"
        data-testid="model-viewer"
        data-phase={phase.kind}
        aria-label="3D model of the board. Arrow keys orbit, plus and minus zoom, F refits."
        role="img"
        tabIndex={0}
      />
      {phase.kind === "loading" ? (
        <p className="absolute inset-x-0 bottom-0 px-2 py-1 text-[10px] text-white/70" data-testid="model-viewer-status">
          Reading the model…
        </p>
      ) : null}
      {phase.kind === "error" ? (
        <p
          className="absolute inset-0 flex items-center justify-center p-3 text-center text-[11px] text-white/85"
          data-testid="model-viewer-error"
        >
          {phase.message}
        </p>
      ) : null}
      {phase.kind === "lost" ? (
        <p
          className="absolute inset-0 flex items-center justify-center p-3 text-center text-[11px] text-white/85"
          data-testid="model-viewer-lost"
        >
          The browser dropped the WebGL context; the model redraws when it comes back.
        </p>
      ) : null}
      {loaded ? (
        <div className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 px-2 py-1 text-[10px] text-white/60">
          <span data-testid="model-viewer-status" title={loaded.warnings.join(" ") || undefined}>
            {loaded.meshes} mesh{loaded.meshes === 1 ? "" : "es"}
            {loaded.warnings.length ? ` · ${loaded.warnings.length} skipped` : ""}
          </span>
          <span className="flex items-center gap-1">
            <span>drag or arrows orbit · wheel zooms · shift-drag pans</span>
            <button
              type="button"
              className="rounded border border-white/20 px-1 hover:bg-white/10"
              onClick={() => rendererRef.current?.fit()}
              data-testid="model-viewer-fit"
            >
              Fit
            </button>
          </span>
        </div>
      ) : null}
    </div>
  );
};
