// @vitest-environment jsdom
//
// The renderer against a recording fake of WebGL2: jsdom has no GPU, so what
// is checked is the call sequence the GPU would receive — opaque meshes drawn
// before blended ones, blending enabled with depth writes off for the second
// pass, a lost context re-uploaded on restore, keyboard orbit redrawing, and
// dispose releasing every listener and buffer.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRenderer } from "./renderer";
import type { GlbScene, GlbMesh } from "./parse";
import { identity } from "./math";

interface FakeGl {
  calls: string[];
  lost: boolean;
  [key: string]: unknown;
}

const CONSTANTS: Record<string, number> = {
  VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
  ARRAY_BUFFER: 5, ELEMENT_ARRAY_BUFFER: 6, STATIC_DRAW: 7, FLOAT: 8,
  TRIANGLES: 9, UNSIGNED_INT: 10, DEPTH_TEST: 11, CULL_FACE: 12, BLEND: 13,
  SRC_ALPHA: 14, ONE_MINUS_SRC_ALPHA: 15, COLOR_BUFFER_BIT: 16, DEPTH_BUFFER_BIT: 32,
};
const NAME_OF = Object.fromEntries(Object.entries(CONSTANTS).map(([k, v]) => [v, k]));

function fakeGl(): FakeGl {
  const gl: FakeGl = { calls: [], lost: false };
  Object.assign(gl, CONSTANTS);
  const record = (name: string, ...args: unknown[]) => {
    const shown = args
      .filter((a) => typeof a === "number" || typeof a === "boolean")
      .map((a) => (typeof a === "number" && NAME_OF[a] ? NAME_OF[a] : String(a)))
      .join(",");
    gl.calls.push(shown ? `${name}(${shown})` : name);
  };
  let objects = 0;
  const make = (name: string) => () => {
    record(name);
    objects += 1;
    return { id: objects };
  };
  gl.createShader = make("createShader");
  gl.createProgram = make("createProgram");
  gl.createVertexArray = make("createVertexArray");
  gl.createBuffer = make("createBuffer");
  gl.shaderSource = () => {};
  gl.compileShader = () => {};
  gl.getShaderParameter = () => true;
  gl.getShaderInfoLog = () => "";
  gl.attachShader = () => {};
  gl.linkProgram = () => {};
  gl.getProgramParameter = () => true;
  gl.getProgramInfoLog = () => "";
  gl.getAttribLocation = (_p: unknown, name: string) => (name === "aPosition" ? 0 : 1);
  gl.getUniformLocation = (_p: unknown, name: string) => ({ name });
  gl.deleteShader = () => {};
  for (const name of [
    "bindVertexArray", "bindBuffer", "bufferData", "enableVertexAttribArray", "vertexAttribPointer",
    "viewport", "clearColor", "enable", "disable", "depthMask", "clear", "useProgram",
    "uniformMatrix4fv", "uniformMatrix3fv", "blendFunc", "drawElements", "drawArrays",
    "deleteVertexArray", "deleteBuffer", "deleteProgram",
  ]) {
    gl[name] = (...args: unknown[]) => record(name, ...args);
  }
  gl.uniform4f = (_loc: unknown, r: number, g: number, b: number, a: number) => gl.calls.push(`color(${r},${g},${b},${a})`);
  gl.isContextLost = () => gl.lost;
  return gl;
}

function mesh(color: [number, number, number, number], blend: boolean, z: number): GlbMesh {
  const matrix = identity();
  matrix[14] = z;
  return {
    positions: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]),
    normals: new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1]),
    indices: new Uint32Array([0, 1, 2]),
    color,
    blend,
    matrix,
  };
}

function scene(meshes: GlbMesh[]): GlbScene {
  return { meshes, bounds: { min: [0, 0, -2], max: [1, 1, 2] }, warnings: [] };
}

let gl: FakeGl;
let canvas: HTMLCanvasElement;
let frames: FrameRequestCallback[];

beforeEach(() => {
  gl = fakeGl();
  frames = [];
  canvas = document.createElement("canvas");
  canvas.getContext = (() => gl) as unknown as HTMLCanvasElement["getContext"];
  canvas.getBoundingClientRect = () => ({ width: 200, height: 100, top: 0, left: 0, right: 200, bottom: 100, x: 0, y: 0, toJSON() {} });
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => vi.unstubAllGlobals());

const flush = () => {
  const pending = frames.splice(0);
  for (const cb of pending) cb(0);
};

describe("createRenderer", () => {
  it("refuses a canvas with no WebGL2 with a sentence that names the fallback", () => {
    canvas.getContext = (() => null) as unknown as HTMLCanvasElement["getContext"];
    expect(() => createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]))).toThrow(/no WebGL2 context.*external viewer/);
  });

  it("sizes the canvas from its CSS box and draws once per request", () => {
    const renderer = createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]));
    expect(canvas.width).toBe(200);
    expect(canvas.height).toBe(100);
    expect(gl.calls.filter((c) => c.startsWith("drawElements"))).toHaveLength(0);
    flush();
    expect(gl.calls.filter((c) => c.startsWith("drawElements"))).toHaveLength(1);
    expect(renderer.drawCount).toBe(1);
  });

  it("draws opaque meshes first, then blended ones back to front with depth writes off", () => {
    // Blended near (z=+1, closer to the camera) and far (z=-1); the far one
    // must be composited first.
    createRenderer(
      canvas,
      scene([
        mesh([0.1, 0.1, 0.1, 0.83], true, 1),
        mesh([1, 0, 0, 1], false, 0),
        mesh([0.2, 0.2, 0.2, 0.9], true, -1),
      ])
    );
    flush();
    const colours = gl.calls.filter((c) => c.startsWith("color("));
    expect(colours).toEqual(["color(1,0,0,1)", "color(0.2,0.2,0.2,0.9)", "color(0.1,0.1,0.1,0.83)"]);
    const first = gl.calls.indexOf("color(1,0,0,1)");
    const blendOn = gl.calls.indexOf("enable(BLEND)");
    const depthOff = gl.calls.indexOf("depthMask(false)");
    const farBlend = gl.calls.indexOf("color(0.2,0.2,0.2,0.9)");
    expect(first).toBeLessThan(blendOn);
    expect(blendOn).toBeLessThan(farBlend);
    expect(depthOff).toBeLessThan(farBlend);
    expect(gl.calls).toContain("blendFunc(SRC_ALPHA,ONE_MINUS_SRC_ALPHA)");
    // The frame leaves the state as it found it.
    const last = gl.calls.length;
    expect(gl.calls.lastIndexOf("depthMask(true)")).toBeGreaterThan(farBlend);
    expect(gl.calls.lastIndexOf("disable(BLEND)")).toBeLessThan(last);
  });

  it("never enables blending when no mesh is translucent", () => {
    createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0), mesh([0, 1, 0, 1], true, 0)]));
    flush();
    expect(gl.calls).not.toContain("enable(BLEND)");
  });

  it("re-uploads and redraws after the context is lost and restored", () => {
    createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]));
    flush();
    const uploadsBefore = gl.calls.filter((c) => c === "createVertexArray").length;
    expect(uploadsBefore).toBe(1);

    const lost = new Event("webglcontextlost", { cancelable: true });
    gl.lost = true;
    canvas.dispatchEvent(lost);
    expect(lost.defaultPrevented).toBe(true);
    gl.calls.length = 0;
    canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
    flush();
    expect(gl.calls.filter((c) => c.startsWith("drawElements"))).toHaveLength(0);

    gl.lost = false;
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    flush();
    expect(gl.calls.filter((c) => c === "createVertexArray")).toHaveLength(1);
    expect(gl.calls.filter((c) => c.startsWith("drawElements"))).toHaveLength(1);
  });

  it("reports loss and restoration to the caller", () => {
    const onContextLost = vi.fn();
    const onContextRestored = vi.fn();
    createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]), { onContextLost, onContextRestored });
    canvas.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    expect(onContextLost).toHaveBeenCalledTimes(1);
    expect(onContextRestored).not.toHaveBeenCalled();
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    expect(onContextRestored).toHaveBeenCalledTimes(1);
  });

  it("orbits on arrow keys and swallows them, and leaves other keys alone", () => {
    createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]));
    flush();
    const before = gl.calls.filter((c) => c.startsWith("uniformMatrix4fv")).length;
    const arrow = new KeyboardEvent("keydown", { key: "ArrowRight", cancelable: true });
    canvas.dispatchEvent(arrow);
    expect(arrow.defaultPrevented).toBe(true);
    flush();
    expect(gl.calls.filter((c) => c.startsWith("uniformMatrix4fv")).length).toBeGreaterThan(before);

    const tab = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
    canvas.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBe(false);
    expect(frames).toHaveLength(0);
  });

  it("dispose releases the GPU objects and stops listening", () => {
    const renderer = createRenderer(canvas, scene([mesh([1, 0, 0, 1], false, 0)]));
    flush();
    renderer.dispose();
    expect(gl.calls.filter((c) => c === "deleteVertexArray")).toHaveLength(1);
    expect(gl.calls.filter((c) => c === "deleteBuffer")).toHaveLength(3);
    expect(gl.calls).toContain("deleteProgram");
    gl.calls.length = 0;
    canvas.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
    canvas.dispatchEvent(new Event("webglcontextrestored"));
    flush();
    expect(gl.calls).toEqual([]);
    renderer.dispose();
  });
});
