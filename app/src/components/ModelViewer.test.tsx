// @vitest-environment jsdom
//
// The viewer's lifecycle around a mocked file read and a mocked renderer:
// the phase the canvas advertises, the sentence shown when the webview has
// no WebGL, and the leak the review found — a renderer created after the
// effect was cancelled (a fast path change) used to outlive its effect.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-fs", () => ({ readFile: vi.fn() }));
vi.mock("@/lib/gltf", () => ({
  parseGlb: vi.fn(),
  createRenderer: vi.fn(),
  GlbError: class GlbError extends Error {},
  RendererError: class RendererError extends Error {},
}));

import { readFile } from "@tauri-apps/plugin-fs";
import { createRenderer, parseGlb } from "@/lib/gltf";
import { ModelViewer } from "./ModelViewer";

const mockRead = vi.mocked(readFile);
const mockParse = vi.mocked(parseGlb);
const mockCreate = vi.mocked(createRenderer);

type Deferred = { promise: Promise<Uint8Array>; resolve: (b: Uint8Array) => void };
function deferred(): Deferred {
  let resolve!: (b: Uint8Array) => void;
  const promise = new Promise<Uint8Array>((r) => { resolve = r; });
  return { promise, resolve };
}

function fakeRenderer() {
  return { resize: vi.fn(), fit: vi.fn(), drawCount: 1, dispose: vi.fn() };
}

const SCENE = { meshes: [{}, {}], bounds: { min: [0, 0, 0], max: [1, 1, 1] }, warnings: [] };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ModelViewer", () => {
  it("reports loading, then ready with the mesh count", async () => {
    const read = deferred();
    mockRead.mockReturnValue(read.promise);
    mockParse.mockReturnValue(SCENE as never);
    mockCreate.mockReturnValue(fakeRenderer());
    render(<ModelViewer path="/tmp/a.glb" />);
    const canvas = screen.getByTestId("model-viewer");
    expect(canvas.getAttribute("data-phase")).toBe("loading");
    expect(canvas.getAttribute("tabindex")).toBe("0");
    await act(async () => read.resolve(new Uint8Array(4)));
    await waitFor(() => expect(canvas.getAttribute("data-phase")).toBe("ready"));
    expect(screen.getByTestId("model-viewer-status").textContent).toContain("2 meshes");
    expect(mockCreate).toHaveBeenCalledTimes(1);
  });

  it("shows the renderer's sentence when the window has no WebGL2, and tells the caller", async () => {
    mockRead.mockResolvedValue(new Uint8Array(4));
    mockParse.mockReturnValue(SCENE as never);
    mockCreate.mockImplementation(() => {
      throw new Error("This window has no WebGL2 context, so the model cannot be drawn here; open the file in an external viewer instead.");
    });
    const onError = vi.fn();
    render(<ModelViewer path="/tmp/a.glb" onError={onError} />);
    await waitFor(() => expect(screen.getByTestId("model-viewer-error")).toBeTruthy());
    expect(screen.getByTestId("model-viewer-error").textContent).toContain("no WebGL2 context");
    expect(screen.getByTestId("model-viewer").getAttribute("data-phase")).toBe("error");
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("names the path when the file cannot be read", async () => {
    mockRead.mockRejectedValue(new Error("forbidden path"));
    render(<ModelViewer path="/tmp/missing.glb" />);
    await waitFor(() => expect(screen.getByTestId("model-viewer-error")).toBeTruthy());
    const text = screen.getByTestId("model-viewer-error").textContent ?? "";
    expect(text).toContain("/tmp/missing.glb");
    expect(text).toContain("forbidden path");
    expect(mockCreate).not.toHaveBeenCalled();
  });

  it("attaches exactly one renderer to the canvas when the path changes mid-read", async () => {
    const first = deferred();
    const second = deferred();
    mockRead.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    mockParse.mockReturnValue(SCENE as never);
    const renderer = fakeRenderer();
    mockCreate.mockReturnValue(renderer);

    const view = render(<ModelViewer path="/tmp/a.glb" />);
    view.rerender(<ModelViewer path="/tmp/b.glb" />);
    // The first read finishes after its effect was cleaned up: no renderer
    // may come out of it, or two orbits would fight for one canvas.
    await act(async () => first.resolve(new Uint8Array(4)));
    expect(mockCreate).not.toHaveBeenCalled();
    await act(async () => second.resolve(new Uint8Array(4)));
    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
    expect(renderer.dispose).not.toHaveBeenCalled();
    expect(screen.getByTestId("model-viewer").getAttribute("data-phase")).toBe("ready");

    view.unmount();
    expect(renderer.dispose).toHaveBeenCalledTimes(1);
  });

  it("disposes a renderer created after its effect was cancelled", async () => {
    // parseGlb is the last synchronous step before createRenderer; a parse
    // that swaps the path models a cleanup landing inside that window.
    const read = deferred();
    mockRead.mockReturnValueOnce(read.promise).mockReturnValue(new Promise(() => {}));
    const renderer = fakeRenderer();
    mockCreate.mockReturnValue(renderer);
    let view: ReturnType<typeof render> | null = null;
    mockParse.mockImplementationOnce(() => {
      view?.rerender(<ModelViewer path="/tmp/b.glb" />);
      return SCENE as never;
    });
    view = render(<ModelViewer path="/tmp/a.glb" />);
    await act(async () => read.resolve(new Uint8Array(4)));
    await waitFor(() => expect(mockParse).toHaveBeenCalledTimes(1));
    // Whether React ran the cleanup inside the parse or right after it, no
    // renderer from the cancelled effect survives it.
    await waitFor(() => expect(renderer.dispose).toHaveBeenCalledTimes(mockCreate.mock.calls.length));
    expect(screen.getByTestId("model-viewer").getAttribute("data-phase")).toBe("loading");
  });

  it("shows the lost-context sentence and recovers when the renderer restores", async () => {
    mockRead.mockResolvedValue(new Uint8Array(4));
    mockParse.mockReturnValue(SCENE as never);
    mockCreate.mockReturnValue(fakeRenderer());
    render(<ModelViewer path="/tmp/a.glb" />);
    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(1));
    const options = mockCreate.mock.calls[0][2];
    await act(async () => options?.onContextLost?.());
    expect(screen.getByTestId("model-viewer").getAttribute("data-phase")).toBe("lost");
    expect(screen.getByTestId("model-viewer-lost").textContent).toContain("dropped the WebGL context");
    await act(async () => options?.onContextRestored?.());
    expect(screen.getByTestId("model-viewer").getAttribute("data-phase")).toBe("ready");
  });
});
