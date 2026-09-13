// A WebGL2 renderer for a parsed `GlbScene`: one shader, one VAO per drawn
// mesh, a fixed three-point light in view space, and an orbit camera that
// auto-fits the scene's bounds. It draws on demand (after input or a resize),
// not in a loop, because an overlay that spins a GPU while the engineer works
// in KiCad is a fan they can hear.
//
// Handedness: the camera's up vector is +Y, glTF's and kicad-cli's convention.
// There is no axis flip in this file; a board that looks upside down is a
// bug upstream, not something to correct here.
//
// Transparency: kicad-cli marks the soldermask, silkscreen and board body
// `alphaMode: BLEND`, and the tracks and vias sit underneath the mask. Every
// frame is therefore two passes — opaque meshes with depth writes, then the
// blended ones back to front with depth writes off — so the copper shows
// through the mask the way KiCad's own viewer draws it.

import { lookAt, multiply, normalMatrix, perspective, transformPoint } from "./math";
import type { Vec3 } from "./math";
import type { GlbScene } from "./parse";

export class RendererError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RendererError";
  }
}

export interface Renderer {
  /** Re-reads the canvas's CSS size and redraws. */
  resize(): void;
  /** Frames the whole model again (the initial view). */
  fit(): void;
  /** Number of draw calls per frame — one per mesh instance. */
  readonly drawCount: number;
  dispose(): void;
}

export interface RendererOptions {
  /**
   * Called when the browser drops the WebGL context. The canvas is blank
   * until `onContextRestored` fires; the renderer re-uploads on its own.
   */
  onContextLost?: () => void;
  /** Called after a lost context came back and the scene was re-uploaded. */
  onContextRestored?: () => void;
}

const VERTEX_SHADER = `#version 300 es
in vec3 aPosition;
in vec3 aNormal;
uniform mat4 uProjection;
uniform mat4 uModelView;
uniform mat3 uNormal;
out vec3 vNormal;
void main() {
  vNormal = uNormal * aNormal;
  gl_Position = uProjection * uModelView * vec4(aPosition, 1.0);
}`;

// Three-point lighting in view space: a key above-left of the camera, a
// softer fill from the right, and a rim from behind-above so edges against
// the background read. Two-sided, because kicad-cli marks every material
// doubleSided and a board's underside is half the point of a 3D view.
const FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec3 vNormal;
uniform vec4 uColor;
out vec4 outColor;
const vec3 KEY = normalize(vec3(-0.5, 0.8, 0.6));
const vec3 FILL = normalize(vec3(0.7, 0.2, 0.5));
const vec3 RIM = normalize(vec3(0.2, 0.6, -0.8));
void main() {
  vec3 n = normalize(vNormal);
  if (!gl_FrontFacing) n = -n;
  float light = 0.22
    + 0.62 * max(dot(n, KEY), 0.0)
    + 0.28 * max(dot(n, FILL), 0.0)
    + 0.18 * max(dot(n, RIM), 0.0);
  outColor = vec4(uColor.rgb * min(light, 1.0), uColor.a);
}`;

/** Orbit step per arrow-key press, in radians. */
const KEY_STEP = Math.PI / 36;

interface DrawItem {
  vao: WebGLVertexArrayObject;
  count: number;
  color: [number, number, number, number];
  blend: boolean;
  matrix: Float32Array;
  normal: Float32Array;
  /** World-space centre of the mesh's bounds, for back-to-front sorting. */
  centre: Vec3;
  buffers: WebGLBuffer[];
}

interface Program {
  program: WebGLProgram;
  uProjection: WebGLUniformLocation | null;
  uModelView: WebGLUniformLocation | null;
  uNormal: WebGLUniformLocation | null;
  uColor: WebGLUniformLocation | null;
  items: DrawItem[];
}

/**
 * Face normals for a mesh that carries none: the geometry is un-indexed so
 * each triangle owns its three vertices and can carry one normal.
 */
function flatShade(positions: Float32Array, indices: Uint32Array): { positions: Float32Array; normals: Float32Array } {
  const out = new Float32Array(indices.length * 3);
  const normals = new Float32Array(indices.length * 3);
  for (let t = 0; t < indices.length; t += 3) {
    const a = indices[t] * 3, b = indices[t + 1] * 3, c = indices[t + 2] * 3;
    const ux = positions[b] - positions[a], uy = positions[b + 1] - positions[a + 1], uz = positions[b + 2] - positions[a + 2];
    const vx = positions[c] - positions[a], vy = positions[c + 1] - positions[a + 1], vz = positions[c + 2] - positions[a + 2];
    let nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    for (let k = 0; k < 3; k += 1) {
      const src = indices[t + k] * 3;
      const dst = (t + k) * 3;
      out[dst] = positions[src]; out[dst + 1] = positions[src + 1]; out[dst + 2] = positions[src + 2];
      normals[dst] = nx; normals[dst + 1] = ny; normals[dst + 2] = nz;
    }
  }
  return { positions: out, normals };
}

function meshCentre(positions: Float32Array, matrix: Float32Array): Vec3 {
  const min: Vec3 = [Infinity, Infinity, Infinity];
  const max: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < positions.length; i += 3) {
    for (let k = 0; k < 3; k += 1) {
      const v = positions[i + k];
      if (v < min[k]) min[k] = v;
      if (v > max[k]) max[k] = v;
    }
  }
  return transformPoint(matrix, [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2]);
}

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new RendererError("WebGL could not create a shader object.");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? "no log";
    gl.deleteShader(shader);
    throw new RendererError(`A shader failed to compile: ${log.trim()}`);
  }
  return shader;
}

/** Compiles the shader and uploads every mesh; run again after a context restore. */
function build(gl: WebGL2RenderingContext, scene: GlbScene): Program {
  const program = gl.createProgram();
  if (!program) throw new RendererError("WebGL could not create a program.");
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new RendererError(`The shader program failed to link: ${(gl.getProgramInfoLog(program) ?? "").trim()}`);
  }
  const aPosition = gl.getAttribLocation(program, "aPosition");
  const aNormal = gl.getAttribLocation(program, "aNormal");

  const items: DrawItem[] = [];
  for (const mesh of scene.meshes) {
    if (!mesh.positions.length || !mesh.indices.length) continue;
    const vao = gl.createVertexArray();
    if (!vao) throw new RendererError("WebGL could not create a vertex array.");
    gl.bindVertexArray(vao);
    const buffers: WebGLBuffer[] = [];
    const upload = (target: number, data: ArrayBufferView) => {
      const buffer = gl.createBuffer();
      if (!buffer) throw new RendererError("WebGL could not allocate a buffer.");
      gl.bindBuffer(target, buffer);
      gl.bufferData(target, data, gl.STATIC_DRAW);
      buffers.push(buffer);
    };
    let count: number;
    if (mesh.normals) {
      upload(gl.ARRAY_BUFFER, mesh.positions);
      gl.enableVertexAttribArray(aPosition);
      gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 0, 0);
      upload(gl.ARRAY_BUFFER, mesh.normals);
      gl.enableVertexAttribArray(aNormal);
      gl.vertexAttribPointer(aNormal, 3, gl.FLOAT, false, 0, 0);
      upload(gl.ELEMENT_ARRAY_BUFFER, mesh.indices);
      count = mesh.indices.length;
    } else {
      const flat = flatShade(mesh.positions, mesh.indices);
      upload(gl.ARRAY_BUFFER, flat.positions);
      gl.enableVertexAttribArray(aPosition);
      gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 0, 0);
      upload(gl.ARRAY_BUFFER, flat.normals);
      gl.enableVertexAttribArray(aNormal);
      gl.vertexAttribPointer(aNormal, 3, gl.FLOAT, false, 0, 0);
      count = -flat.positions.length / 3; // negative marks a non-indexed draw
    }
    gl.bindVertexArray(null);
    items.push({
      vao,
      count,
      color: mesh.color,
      blend: mesh.blend && mesh.color[3] < 1,
      matrix: mesh.matrix,
      normal: normalMatrix(mesh.matrix),
      centre: meshCentre(mesh.positions, mesh.matrix),
      buffers,
    });
  }

  return {
    program,
    uProjection: gl.getUniformLocation(program, "uProjection"),
    uModelView: gl.getUniformLocation(program, "uModelView"),
    uNormal: gl.getUniformLocation(program, "uNormal"),
    uColor: gl.getUniformLocation(program, "uColor"),
    items,
  };
}

function release(gl: WebGL2RenderingContext, built: Program) {
  for (const item of built.items) {
    gl.deleteVertexArray(item.vao);
    for (const buffer of item.buffers) gl.deleteBuffer(buffer);
  }
  gl.deleteProgram(built.program);
}

export function createRenderer(canvas: HTMLCanvasElement, scene: GlbScene, options: RendererOptions = {}): Renderer {
  const gl = canvas.getContext("webgl2", { antialias: true, alpha: false, preserveDrawingBuffer: false });
  if (!gl) {
    throw new RendererError(
      "This window has no WebGL2 context, so the model cannot be drawn here; open the file in an external viewer instead."
    );
  }

  let built: Program | null = build(gl, scene);
  const drawCount = built.items.length;

  // --- camera ---------------------------------------------------------------
  const { min, max } = scene.bounds;
  const centre: Vec3 = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
  const radius = Math.max(Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2, 1e-9);
  const FOV = Math.PI / 5;
  const target: Vec3 = [...centre];
  let distance = radius / Math.sin(FOV / 2);
  // A three-quarter view from above: the board face and one edge, the way a
  // part is held up to look at.
  let yaw = Math.PI / 5;
  let pitch = Math.PI / 5;

  const fit = () => {
    target[0] = centre[0]; target[1] = centre[1]; target[2] = centre[2];
    distance = radius / Math.sin(FOV / 2);
    yaw = Math.PI / 5;
    pitch = Math.PI / 5;
    requestDraw();
  };

  const eye = (): Vec3 => [
    target[0] + distance * Math.cos(pitch) * Math.sin(yaw),
    target[1] + distance * Math.sin(pitch),
    target[2] + distance * Math.cos(pitch) * Math.cos(yaw),
  ];

  const orbit = (dYaw: number, dPitch: number) => {
    yaw -= dYaw;
    pitch = Math.min(Math.PI / 2 - 0.01, Math.max(-Math.PI / 2 + 0.01, pitch + dPitch));
    requestDraw();
  };

  const zoom = (factor: number) => {
    distance = Math.min(radius * 50, Math.max(radius * 0.05, distance * factor));
    requestDraw();
  };

  // --- drawing --------------------------------------------------------------
  let disposed = false;
  let frame: number | null = null;
  const requestDraw = () => {
    if (disposed || frame !== null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      draw();
    });
  };

  const draw = () => {
    if (disposed || built === null || gl.isContextLost()) return;
    const width = canvas.width, height = canvas.height;
    if (!width || !height) return;
    const { program, uProjection, uModelView, uNormal, uColor, items } = built;
    gl.viewport(0, 0, width, height);
    gl.clearColor(0.11, 0.12, 0.14, 1);
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.disable(gl.BLEND);
    gl.depthMask(true);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(program);

    const near = Math.max(distance - radius * 2, radius * 0.005);
    const far = distance + radius * 4;
    gl.uniformMatrix4fv(uProjection, false, perspective(FOV, width / height, near, far));
    const view = lookAt(eye(), target, [0, 1, 0]);
    const viewNormal = normalMatrix(view);

    const drawItem = (item: DrawItem) => {
      gl.bindVertexArray(item.vao);
      gl.uniformMatrix4fv(uModelView, false, multiply(view, item.matrix));
      gl.uniformMatrix3fv(uNormal, false, mul3(viewNormal, item.normal));
      gl.uniform4f(uColor, item.color[0], item.color[1], item.color[2], item.color[3]);
      if (item.count >= 0) gl.drawElements(gl.TRIANGLES, item.count, gl.UNSIGNED_INT, 0);
      else gl.drawArrays(gl.TRIANGLES, 0, -item.count);
    };

    // Pass one: everything opaque, writing depth.
    const blended: DrawItem[] = [];
    for (const item of items) {
      if (item.blend) blended.push(item);
      else drawItem(item);
    }

    // Pass two: translucent meshes farthest first, testing depth against the
    // opaque pass but not writing it, so overlapping mask and body composite
    // instead of occluding each other.
    if (blended.length) {
      const depth = (item: DrawItem) => transformPoint(view, item.centre)[2];
      blended.sort((a, b) => depth(a) - depth(b));
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(false);
      for (const item of blended) drawItem(item);
      gl.depthMask(true);
      gl.disable(gl.BLEND);
    }
    gl.bindVertexArray(null);
  };

  const resize = () => {
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    requestDraw();
  };

  // --- input ----------------------------------------------------------------
  // Left drag orbits, shift/right/middle drag pans, wheel zooms, arrow keys
  // orbit when the canvas has focus. Pan speed is tied to the distance so a
  // pixel of drag moves the target by about the world size it covers on
  // screen.
  let dragging: "orbit" | "pan" | null = null;
  let lastX = 0, lastY = 0;
  const onPointerDown = (event: PointerEvent) => {
    dragging = event.button === 0 && !event.shiftKey ? "orbit" : "pan";
    lastX = event.clientX; lastY = event.clientY;
    canvas.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };
  const onPointerMove = (event: PointerEvent) => {
    if (!dragging) return;
    const dx = event.clientX - lastX, dy = event.clientY - lastY;
    lastX = event.clientX; lastY = event.clientY;
    if (dragging === "orbit") {
      orbit(dx * 0.01, dy * 0.01);
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const worldPerPixel = (2 * distance * Math.tan(FOV / 2)) / Math.max(1, rect.height);
    // Camera right and up axes from the yaw/pitch, so the pan stays in the screen plane.
    const rx = Math.cos(yaw), rz = -Math.sin(yaw);
    const ux = -Math.sin(pitch) * Math.sin(yaw), uy = Math.cos(pitch), uz = -Math.sin(pitch) * Math.cos(yaw);
    target[0] += (-dx * rx + dy * ux) * worldPerPixel;
    target[1] += dy * uy * worldPerPixel;
    target[2] += (-dx * rz + dy * uz) * worldPerPixel;
    requestDraw();
  };
  const onPointerUp = (event: PointerEvent) => {
    dragging = null;
    canvas.releasePointerCapture?.(event.pointerId);
  };
  const onWheel = (event: WheelEvent) => {
    event.preventDefault();
    zoom(Math.exp(event.deltaY * 0.0015));
  };
  const onKeyDown = (event: KeyboardEvent) => {
    switch (event.key) {
      case "ArrowLeft": orbit(-KEY_STEP, 0); break;
      case "ArrowRight": orbit(KEY_STEP, 0); break;
      case "ArrowUp": orbit(0, KEY_STEP); break;
      case "ArrowDown": orbit(0, -KEY_STEP); break;
      case "+": case "=": zoom(0.8); break;
      case "-": case "_": zoom(1.25); break;
      case "f": case "F": case "Home": fit(); break;
      default: return;
    }
    event.preventDefault();
  };
  const onContextMenu = (event: Event) => event.preventDefault();

  // preventDefault on the lost event is what tells the browser the page can
  // handle a restore; without it no `webglcontextrestored` ever arrives.
  const onLost = (event: Event) => {
    event.preventDefault();
    if (frame !== null) {
      cancelAnimationFrame(frame);
      frame = null;
    }
    built = null;
    options.onContextLost?.();
  };
  const onRestored = () => {
    if (disposed) return;
    built = build(gl, scene);
    options.onContextRestored?.();
    requestDraw();
  };

  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("keydown", onKeyDown);
  canvas.addEventListener("contextmenu", onContextMenu);
  canvas.addEventListener("webglcontextlost", onLost);
  canvas.addEventListener("webglcontextrestored", onRestored);

  const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
  observer?.observe(canvas);
  resize();

  return {
    resize,
    fit,
    drawCount,
    dispose() {
      if (disposed) return;
      disposed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      observer?.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("keydown", onKeyDown);
      canvas.removeEventListener("contextmenu", onContextMenu);
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", onRestored);
      if (built !== null && !gl.isContextLost()) release(gl, built);
      built = null;
    },
  };
}

/** 3x3 column-major product `a * b`. */
function mul3(a: Float32Array, b: Float32Array): Float32Array {
  const out = new Float32Array(9);
  for (let col = 0; col < 3; col += 1) {
    for (let row = 0; row < 3; row += 1) {
      out[col * 3 + row] = a[row] * b[col * 3] + a[3 + row] * b[col * 3 + 1] + a[6 + row] * b[col * 3 + 2];
    }
  }
  return out;
}
