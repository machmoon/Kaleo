// The GLB reader against files built here byte by byte, in the shapes
// kicad-cli 10 actually writes (uint16 indices, byteStride 12 vertex views,
// translation-only nodes under a root, BLEND materials) plus the refusals
// the header of parse.ts promises: a truncated file, a required extension,
// a sparse accessor, a broken index list. Nothing here touches WebGL.

import { describe, expect, it } from "vitest";
import { GlbError, parseGlb } from "./parse";

// --- a tiny GLB writer -------------------------------------------------------

function pad4(bytes: Uint8Array, fill: number): Uint8Array {
  const rest = (4 - (bytes.byteLength % 4)) % 4;
  if (!rest) return bytes;
  const out = new Uint8Array(bytes.byteLength + rest);
  out.set(bytes);
  out.fill(fill, bytes.byteLength);
  return out;
}

function concat(parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.byteLength, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.byteLength;
  }
  return out;
}

/** Packs `json` and an optional BIN chunk into a version-2 GLB container. */
function glb(json: unknown, bin?: Uint8Array): Uint8Array {
  const jsonBytes = pad4(new TextEncoder().encode(JSON.stringify(json)), 0x20);
  const chunks: Uint8Array[] = [chunk(0x4e4f534a, jsonBytes)];
  if (bin) chunks.push(chunk(0x004e4942, pad4(bin, 0)));
  const body = concat(chunks);
  const header = new Uint8Array(12);
  const view = new DataView(header.buffer);
  view.setUint32(0, 0x46546c67, true);
  view.setUint32(4, 2, true);
  view.setUint32(8, 12 + body.byteLength, true);
  return concat([header, body]);
}

function chunk(type: number, data: Uint8Array): Uint8Array {
  const head = new Uint8Array(8);
  const view = new DataView(head.buffer);
  view.setUint32(0, data.byteLength, true);
  view.setUint32(4, type, true);
  return concat([head, data]);
}

/** A BIN chunk laid out the way kicad-cli does: interleaved-free VEC3 views with byteStride 12, then uint16 indices. */
function triangleBin(indices16 = true): { bin: Uint8Array; positions: number; normals: number; indices: number; indexType: number } {
  const positions = new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]);
  const normals = new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1]);
  const indexBytes = indices16
    ? new Uint8Array(new Uint16Array([0, 1, 2]).buffer)
    : new Uint8Array(new Uint32Array([0, 1, 2]).buffer);
  const bin = concat([
    new Uint8Array(positions.buffer),
    new Uint8Array(normals.buffer),
    pad4(indexBytes, 0),
  ]);
  return {
    bin,
    positions: 0,
    normals: positions.byteLength,
    indices: positions.byteLength + normals.byteLength,
    indexType: indices16 ? 5123 : 5125,
  };
}

interface Overrides {
  nodes?: unknown[];
  scenes?: unknown[];
  scene?: number;
  materials?: unknown[];
  meshes?: unknown[];
  accessors?: unknown[];
  extensionsRequired?: string[];
}

/** One triangle, a translation-only node under a root, the exact kicad-cli shape. */
function triangleGlb(overrides: Overrides = {}, indices16 = true): Uint8Array {
  const layout = triangleBin(indices16);
  const json = {
    asset: { version: "2.0" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [
      { name: "root", children: [1] },
      { name: "part", mesh: 0, translation: [10, 0, 0] },
    ],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, material: 0, mode: 4 }] }],
    materials: [{ pbrMetallicRoughness: { baseColorFactor: [0.2, 0.6, 0.3, 1] } }],
    accessors: [
      { bufferView: 0, componentType: 5126, count: 3, type: "VEC3" },
      { bufferView: 1, componentType: 5126, count: 3, type: "VEC3" },
      { bufferView: 2, componentType: layout.indexType, count: 3, type: "SCALAR" },
    ],
    bufferViews: [
      { buffer: 0, byteOffset: layout.positions, byteLength: 36, byteStride: 12 },
      { buffer: 0, byteOffset: layout.normals, byteLength: 36, byteStride: 12 },
      { buffer: 0, byteOffset: layout.indices, byteLength: indices16 ? 6 : 12 },
    ],
    buffers: [{ byteLength: layout.bin.byteLength }],
    ...overrides,
  };
  return glb(json, layout.bin);
}

// --- tests -------------------------------------------------------------------

describe("parseGlb on kicad-cli shaped files", () => {
  it("reads a strided VEC3 view, uint16 indices and a translation-only node under a root", () => {
    const scene = parseGlb(triangleGlb());
    expect(scene.meshes).toHaveLength(1);
    expect(scene.warnings).toEqual([]);
    const mesh = scene.meshes[0];
    expect(Array.from(mesh.positions)).toEqual([0, 0, 0, 1, 0, 0, 0, 1, 0]);
    expect(Array.from(mesh.indices)).toEqual([0, 1, 2]);
    expect(mesh.normals && Array.from(mesh.normals)).toEqual([0, 0, 1, 0, 0, 1, 0, 0, 1]);
    expect(mesh.color).toEqual([0.2, 0.6, 0.3, 1]);
    expect(mesh.blend).toBe(false);
    // The node's translation reaches the world matrix and therefore the bounds.
    expect(mesh.matrix[12]).toBe(10);
    expect(scene.bounds.min).toEqual([10, 0, 0]);
    expect(scene.bounds.max).toEqual([11, 1, 0]);
  });

  it("reads uint32 indices the same way", () => {
    const scene = parseGlb(triangleGlb({}, false));
    expect(Array.from(scene.meshes[0].indices)).toEqual([0, 1, 2]);
  });

  it("flags a BLEND material and keeps its alpha instead of rounding it to opaque", () => {
    const scene = parseGlb(
      triangleGlb({ materials: [{ alphaMode: "BLEND", pbrMetallicRoughness: { baseColorFactor: [0.1, 0.4, 0.2, 0.83] } }] })
    );
    expect(scene.meshes[0].blend).toBe(true);
    expect(scene.meshes[0].color[3]).toBeCloseTo(0.83);
  });

  it("treats OPAQUE and MASK as not blended", () => {
    for (const alphaMode of ["OPAQUE", "MASK", undefined]) {
      const scene = parseGlb(triangleGlb({ materials: [{ alphaMode, pbrMetallicRoughness: { baseColorFactor: [1, 1, 1, 0.5] } }] }));
      expect(scene.meshes[0].blend).toBe(false);
    }
  });

  it("instances one mesh under two nodes, sharing the decoded geometry and differing only in matrix", () => {
    const scene = parseGlb(
      triangleGlb({
        scenes: [{ nodes: [0] }],
        nodes: [
          { children: [1, 2] },
          { mesh: 0, translation: [0, 0, 0] },
          { mesh: 0, translation: [5, 0, 0], rotation: [0, 0, 0, 1], scale: [2, 2, 2] },
        ],
      })
    );
    expect(scene.meshes).toHaveLength(2);
    expect(scene.meshes[0].positions).toBe(scene.meshes[1].positions);
    expect(scene.meshes[1].matrix[12]).toBe(5);
    expect(scene.meshes[1].matrix[0]).toBe(2);
    expect(scene.bounds.max).toEqual([7, 2, 0]);
  });

  it("applies a quaternion rotation through the node matrix", () => {
    // 90 degrees about +Y: (1,0,0) -> (0,0,-1).
    const s = Math.SQRT1_2;
    const scene = parseGlb(triangleGlb({ nodes: [{ children: [1] }, { mesh: 0, rotation: [0, s, 0, s] }] }));
    expect(scene.bounds.min[2]).toBeCloseTo(-1, 5);
    expect(scene.bounds.max[0]).toBeCloseTo(0, 5);
  });

  it("counts a non-triangle primitive in warnings rather than drawing it wrong", () => {
    const scene = parseGlb(
      triangleGlb({
        meshes: [
          {
            primitives: [
              { attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, mode: 4 },
              { attributes: { POSITION: 0 }, indices: 2, mode: 1 },
            ],
          },
        ],
      })
    );
    expect(scene.meshes).toHaveLength(1);
    expect(scene.warnings).toEqual(["1 non-triangle primitive was skipped (only TRIANGLES are drawn)."]);
  });

  it("defaults a primitive with no material to opaque white", () => {
    const scene = parseGlb(triangleGlb({ meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 2 }] }] }));
    expect(scene.meshes[0].color).toEqual([1, 1, 1, 1]);
    expect(scene.meshes[0].blend).toBe(false);
    expect(scene.meshes[0].normals).toBeNull();
  });
});

describe("parseGlb refusals", () => {
  const refuse = (bytes: Uint8Array, pattern: RegExp) => {
    expect(() => parseGlb(bytes)).toThrow(GlbError);
    expect(() => parseGlb(bytes)).toThrow(pattern);
  };

  it("refuses a file shorter than the header", () => {
    refuse(new Uint8Array(7), /too short/);
  });

  it("refuses a file that is not a GLB at all", () => {
    refuse(new TextEncoder().encode("solid enclosure\nendsolid"), /glTF magic/);
  });

  it("refuses a truncated file by its own declared length", () => {
    const whole = triangleGlb();
    refuse(whole.subarray(0, whole.byteLength - 20), /truncated/);
  });

  it("refuses a required extension by name", () => {
    refuse(triangleGlb({ extensionsRequired: ["KHR_draco_mesh_compression"] }), /KHR_draco_mesh_compression/);
  });

  it("refuses a sparse accessor", () => {
    refuse(
      triangleGlb({
        accessors: [
          { bufferView: 0, componentType: 5126, count: 3, type: "VEC3", sparse: { count: 1 } },
          { bufferView: 1, componentType: 5126, count: 3, type: "VEC3" },
          { bufferView: 2, componentType: 5123, count: 3, type: "SCALAR" },
        ],
      }),
      /sparse/
    );
  });

  it("refuses an index count that is not a multiple of three", () => {
    refuse(
      triangleGlb({
        accessors: [
          { bufferView: 0, componentType: 5126, count: 3, type: "VEC3" },
          { bufferView: 1, componentType: 5126, count: 3, type: "VEC3" },
          { bufferView: 2, componentType: 5123, count: 2, type: "SCALAR" },
        ],
      }),
      /not a multiple of three/
    );
  });

  it("refuses an index past the vertex count", () => {
    const layout = triangleBin();
    const view = new DataView(layout.bin.buffer);
    view.setUint16(layout.indices + 4, 7, true);
    const bytes = triangleGlb();
    // Rebuild with the corrupted BIN: same JSON, hostile data.
    const jsonEnd = 12 + 8 + new DataView(bytes.buffer, 12).getUint32(0, true);
    const patched = new Uint8Array(bytes);
    patched.set(layout.bin, jsonEnd + 8);
    refuse(patched, /indexes vertex 7/);
  });

  it("refuses a file with no triangle geometry so an empty canvas never reads as loaded", () => {
    refuse(triangleGlb({ meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 2, mode: 0 }] }] }), /no triangle geometry/);
  });

  it("refuses a node tree that is a graph", () => {
    refuse(triangleGlb({ nodes: [{ children: [1] }, { mesh: 0, children: [0] }] }), /appears twice/);
  });
});
