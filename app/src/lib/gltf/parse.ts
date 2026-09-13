// A binary-glTF (.glb) reader for the boards kicad-cli exports, written by
// hand because the overlay ships no three.js and nothing may be installed.
//
// Scope, stated rather than implied: core glTF 2.0 only — the GLB container,
// buffers, bufferViews (with byteStride), accessors (any component type, no
// sparse storage), meshes whose primitives are TRIANGLES, node TRS/matrix
// transforms, and each material's `baseColorFactor` and `alphaMode`. Textures, texture
// coordinates, skins, morph targets, animations, cameras and every extension
// are ignored; a file that *requires* an extension is refused by name rather
// than drawn wrong. Primitives in another mode (lines, points, strips) are
// dropped and counted in `warnings`, and a file with no triangle geometry at
// all throws, so an empty canvas never reads as a loaded model.
//
// Handedness: glTF is right-handed Y-up and kicad-cli writes it that way. The
// scene comes out untouched — no axis flip here or anywhere downstream.

import { fromTRS, identity, multiply, transformPoint } from "./math";
import type { Vec3 } from "./math";

export class GlbError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GlbError";
  }
}

export interface GlbMesh {
  positions: Float32Array;
  normals: Float32Array | null;
  indices: Uint32Array;
  /** RGBA in 0..1, the material's baseColorFactor (glTF's default is white). */
  color: [number, number, number, number];
  /**
   * True when the material's `alphaMode` is `BLEND`: the alpha in `color` is
   * meant to be composited, not ignored. kicad-cli marks the soldermask,
   * silkscreen and board body this way, and drawing them opaque hides every
   * track under the mask.
   */
  blend: boolean;
  /** 4x4 column-major node-to-world transform. */
  matrix: Float32Array;
}

export interface GlbScene {
  meshes: GlbMesh[];
  /** World-space axis-aligned bounds over every drawn vertex. */
  bounds: { min: Vec3; max: Vec3 };
  /** What the reader skipped on purpose, one sentence each. */
  warnings: string[];
}

// --- the JSON chunk, typed only as far as this reader looks -----------------

interface JsonBuffer { byteLength: number; uri?: string }
interface JsonBufferView { buffer: number; byteOffset?: number; byteLength: number; byteStride?: number }
interface JsonAccessor {
  bufferView?: number;
  byteOffset?: number;
  componentType: number;
  normalized?: boolean;
  count: number;
  type: string;
  sparse?: unknown;
}
interface JsonPrimitive { attributes: Record<string, number>; indices?: number; material?: number; mode?: number }
interface JsonMesh { name?: string; primitives: JsonPrimitive[] }
interface JsonNode {
  name?: string;
  children?: number[];
  mesh?: number;
  matrix?: number[];
  translation?: number[];
  rotation?: number[];
  scale?: number[];
}
interface JsonMaterial { alphaMode?: string; pbrMetallicRoughness?: { baseColorFactor?: number[] } }
interface JsonRoot {
  asset?: { version?: string };
  extensionsRequired?: string[];
  scene?: number;
  scenes?: Array<{ nodes?: number[] }>;
  nodes?: JsonNode[];
  meshes?: JsonMesh[];
  accessors?: JsonAccessor[];
  bufferViews?: JsonBufferView[];
  buffers?: JsonBuffer[];
  materials?: JsonMaterial[];
}

const GLB_MAGIC = 0x46546c67; // "glTF"
const CHUNK_JSON = 0x4e4f534a;
const CHUNK_BIN = 0x004e4942;
const MODE_TRIANGLES = 4;

const COMPONENT_BYTES: Record<number, number> = {
  5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4,
};
const TYPE_COMPONENTS: Record<string, number> = {
  SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT2: 4, MAT3: 9, MAT4: 16,
};

function readU32(view: DataView, offset: number): number {
  return view.getUint32(offset, true);
}

function splitGlb(bytes: Uint8Array): { json: JsonRoot; bin: Uint8Array | null } {
  if (bytes.byteLength < 12) throw new GlbError("The file is too short to be a GLB (no 12-byte header).");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (readU32(view, 0) !== GLB_MAGIC) throw new GlbError("The file does not start with the glTF magic; it is not a GLB.");
  const version = readU32(view, 4);
  if (version !== 2) throw new GlbError(`The GLB is container version ${version}; only version 2 is read.`);
  const declared = readU32(view, 8);
  if (declared > bytes.byteLength) {
    throw new GlbError(`The GLB header declares ${declared} bytes but the file has ${bytes.byteLength}; it is truncated.`);
  }

  let json: JsonRoot | null = null;
  let bin: Uint8Array | null = null;
  let offset = 12;
  while (offset + 8 <= declared) {
    const length = readU32(view, offset);
    const type = readU32(view, offset + 4);
    const start = offset + 8;
    if (start + length > declared) throw new GlbError("A GLB chunk runs past the end of the file; it is truncated.");
    const chunk = bytes.subarray(start, start + length);
    if (type === CHUNK_JSON && json === null) {
      try {
        json = JSON.parse(new TextDecoder().decode(chunk)) as JsonRoot;
      } catch {
        throw new GlbError("The GLB's JSON chunk is not valid JSON.");
      }
    } else if (type === CHUNK_BIN && bin === null) {
      bin = chunk;
    }
    offset = start + length;
    offset += (4 - (offset % 4)) % 4;
  }
  if (json === null) throw new GlbError("The GLB has no JSON chunk.");
  return { json, bin };
}

/** Every accessor is read through here; the result is a flat typed array. */
class Accessors {
  constructor(private readonly root: JsonRoot, private readonly bin: Uint8Array | null) {}

  private accessor(index: number, role: string): JsonAccessor {
    const accessor = this.root.accessors?.[index];
    if (!accessor) throw new GlbError(`Accessor ${index} (${role}) is missing from the file.`);
    if (accessor.sparse) throw new GlbError(`Accessor ${index} (${role}) is sparse, which this reader does not support.`);
    if (!(accessor.componentType in COMPONENT_BYTES)) {
      throw new GlbError(`Accessor ${index} (${role}) has unknown componentType ${accessor.componentType}.`);
    }
    if (!(accessor.type in TYPE_COMPONENTS)) {
      throw new GlbError(`Accessor ${index} (${role}) has unknown type ${accessor.type}.`);
    }
    return accessor;
  }

  /** Reads accessor `index` as floats, `normalized` integers scaled to 0..1 / -1..1. */
  read(index: number, role: string, expectedType?: string): { data: Float64Array; components: number; count: number } {
    const accessor = this.accessor(index, role);
    if (expectedType && accessor.type !== expectedType) {
      throw new GlbError(`Accessor ${index} (${role}) is ${accessor.type}; ${expectedType} was expected.`);
    }
    const components = TYPE_COMPONENTS[accessor.type];
    const count = accessor.count;
    const data = new Float64Array(count * components);
    if (accessor.bufferView === undefined) return { data, components, count }; // spec: all zeros

    const bufferView = this.root.bufferViews?.[accessor.bufferView];
    if (!bufferView) throw new GlbError(`Accessor ${index} (${role}) names bufferView ${accessor.bufferView}, which is missing.`);
    const buffer = this.root.buffers?.[bufferView.buffer];
    if (!buffer) throw new GlbError(`BufferView ${accessor.bufferView} names buffer ${bufferView.buffer}, which is missing.`);
    if (buffer.uri !== undefined) {
      throw new GlbError(`Buffer ${bufferView.buffer} points at an external uri; only the embedded BIN chunk is read.`);
    }
    if (bufferView.buffer !== 0 || this.bin === null) {
      throw new GlbError(`Buffer ${bufferView.buffer} has no data: the GLB carries ${this.bin ? "one" : "no"} BIN chunk.`);
    }

    const componentBytes = COMPONENT_BYTES[accessor.componentType];
    const elementBytes = components * componentBytes;
    const stride = bufferView.byteStride ?? elementBytes;
    const base = (bufferView.byteOffset ?? 0) + (accessor.byteOffset ?? 0);
    const needed = count === 0 ? 0 : base + (count - 1) * stride + elementBytes;
    const viewEnd = (bufferView.byteOffset ?? 0) + bufferView.byteLength;
    if (needed > viewEnd || viewEnd > this.bin.byteLength) {
      throw new GlbError(`Accessor ${index} (${role}) reads past the end of its bufferView.`);
    }

    const view = new DataView(this.bin.buffer, this.bin.byteOffset, this.bin.byteLength);
    const readOne = componentReader(accessor.componentType, accessor.normalized === true);
    for (let i = 0; i < count; i += 1) {
      const at = base + i * stride;
      for (let c = 0; c < components; c += 1) {
        data[i * components + c] = readOne(view, at + c * componentBytes);
      }
    }
    return { data, components, count };
  }
}

function componentReader(componentType: number, normalized: boolean): (view: DataView, at: number) => number {
  switch (componentType) {
    case 5120: return normalized ? (v, a) => Math.max(v.getInt8(a) / 127, -1) : (v, a) => v.getInt8(a);
    case 5121: return normalized ? (v, a) => v.getUint8(a) / 255 : (v, a) => v.getUint8(a);
    case 5122: return normalized ? (v, a) => Math.max(v.getInt16(a, true) / 32767, -1) : (v, a) => v.getInt16(a, true);
    case 5123: return normalized ? (v, a) => v.getUint16(a, true) / 65535 : (v, a) => v.getUint16(a, true);
    case 5125: return (v, a) => v.getUint32(a, true);
    default: return (v, a) => v.getFloat32(a, true);
  }
}

function nodeMatrix(node: JsonNode, index: number): Float32Array {
  if (node.matrix) {
    if (node.matrix.length !== 16) throw new GlbError(`Node ${index} has a matrix with ${node.matrix.length} values, not 16.`);
    return new Float32Array(node.matrix);
  }
  const t = node.translation, r = node.rotation, s = node.scale;
  if (t && t.length !== 3) throw new GlbError(`Node ${index} has a translation with ${t.length} values, not 3.`);
  if (r && r.length !== 4) throw new GlbError(`Node ${index} has a rotation with ${r.length} values, not 4.`);
  if (s && s.length !== 3) throw new GlbError(`Node ${index} has a scale with ${s.length} values, not 3.`);
  return fromTRS(
    t as Vec3 | undefined,
    r as [number, number, number, number] | undefined,
    s as Vec3 | undefined
  );
}

function materialColor(
  root: JsonRoot,
  index: number | undefined
): { color: [number, number, number, number]; blend: boolean } {
  if (index === undefined) return { color: [1, 1, 1, 1], blend: false };
  const material = root.materials?.[index];
  if (!material) throw new GlbError(`Material ${index} is missing from the file.`);
  const blend = material.alphaMode === "BLEND";
  const factor = material.pbrMetallicRoughness?.baseColorFactor;
  if (!factor) return { color: [1, 1, 1, 1], blend };
  if (factor.length !== 4) throw new GlbError(`Material ${index} has a baseColorFactor with ${factor.length} values, not 4.`);
  return { color: [factor[0], factor[1], factor[2], factor[3]], blend };
}

/**
 * Parses a GLB into flat, world-transformed draw lists. Throws `GlbError`
 * with a sentence naming what was wrong; never returns a partial scene.
 */
export function parseGlb(bytes: Uint8Array): GlbScene {
  const { json, bin } = splitGlb(bytes);
  if (json.asset?.version !== undefined && !String(json.asset.version).startsWith("2.")) {
    throw new GlbError(`The asset is glTF ${json.asset.version}; only 2.x is read.`);
  }
  if (json.extensionsRequired?.length) {
    throw new GlbError(`The file requires the extension${json.extensionsRequired.length === 1 ? "" : "s"} ${json.extensionsRequired.join(", ")}, which this reader does not implement.`);
  }

  const accessors = new Accessors(json, bin);
  const warnings: string[] = [];
  const meshes: GlbMesh[] = [];
  const min: Vec3 = [Infinity, Infinity, Infinity];
  const max: Vec3 = [-Infinity, -Infinity, -Infinity];
  let skippedPrimitives = 0;

  // Each (mesh, primitive) pair is decoded once and shared between the nodes
  // that instance it; only the world matrix differs per instance.
  const decoded = new Map<string, Omit<GlbMesh, "matrix">>();
  const decodePrimitive = (meshIndex: number, primitiveIndex: number, primitive: JsonPrimitive) => {
    const key = `${meshIndex}/${primitiveIndex}`;
    const cached = decoded.get(key);
    if (cached) return cached;
    const where = `mesh ${meshIndex} primitive ${primitiveIndex}`;
    const positionIndex = primitive.attributes?.POSITION;
    if (positionIndex === undefined) throw new GlbError(`The ${where} has no POSITION attribute.`);
    const position = accessors.read(positionIndex, `${where} POSITION`, "VEC3");
    const positions = Float32Array.from(position.data);

    let normals: Float32Array | null = null;
    const normalIndex = primitive.attributes.NORMAL;
    if (normalIndex !== undefined) {
      const normal = accessors.read(normalIndex, `${where} NORMAL`, "VEC3");
      if (normal.count !== position.count) {
        throw new GlbError(`The ${where} has ${normal.count} normals for ${position.count} positions.`);
      }
      normals = Float32Array.from(normal.data);
    }

    let indices: Uint32Array;
    if (primitive.indices !== undefined) {
      const index = accessors.read(primitive.indices, `${where} indices`, "SCALAR");
      indices = Uint32Array.from(index.data);
      for (let i = 0; i < indices.length; i += 1) {
        if (indices[i] >= position.count) {
          throw new GlbError(`The ${where} indexes vertex ${indices[i]} but has only ${position.count} vertices.`);
        }
      }
    } else {
      indices = new Uint32Array(position.count);
      for (let i = 0; i < position.count; i += 1) indices[i] = i;
    }
    if (indices.length % 3 !== 0) {
      throw new GlbError(`The ${where} has ${indices.length} indices, not a multiple of three.`);
    }

    const result = { positions, normals, indices, ...materialColor(json, primitive.material) };
    decoded.set(key, result);
    return result;
  };

  const visited = new Set<number>();
  const visit = (nodeIndex: number, parent: Float32Array) => {
    const node = json.nodes?.[nodeIndex];
    if (!node) throw new GlbError(`Node ${nodeIndex} is referenced but missing from the file.`);
    if (visited.has(nodeIndex)) throw new GlbError(`Node ${nodeIndex} appears twice in the node tree; glTF nodes form a tree, not a graph.`);
    visited.add(nodeIndex);
    const world = multiply(parent, nodeMatrix(node, nodeIndex));

    if (node.mesh !== undefined) {
      const mesh = json.meshes?.[node.mesh];
      if (!mesh) throw new GlbError(`Node ${nodeIndex} names mesh ${node.mesh}, which is missing.`);
      mesh.primitives.forEach((primitive, primitiveIndex) => {
        if ((primitive.mode ?? MODE_TRIANGLES) !== MODE_TRIANGLES) {
          skippedPrimitives += 1;
          return;
        }
        const shared = decodePrimitive(node.mesh as number, primitiveIndex, primitive);
        meshes.push({ ...shared, matrix: world });
        const p = shared.positions;
        for (let i = 0; i < p.length; i += 3) {
          const [x, y, z] = transformPoint(world, [p[i], p[i + 1], p[i + 2]]);
          if (x < min[0]) min[0] = x; if (x > max[0]) max[0] = x;
          if (y < min[1]) min[1] = y; if (y > max[1]) max[1] = y;
          if (z < min[2]) min[2] = z; if (z > max[2]) max[2] = z;
        }
      });
    }
    for (const child of node.children ?? []) visit(child, world);
    visited.delete(nodeIndex);
  };

  const sceneIndex = json.scene ?? 0;
  const scene = json.scenes?.[sceneIndex];
  if (json.scenes && !scene) throw new GlbError(`Scene ${sceneIndex} is missing from the file.`);
  let roots: number[];
  if (scene) {
    roots = scene.nodes ?? [];
  } else {
    // No scenes at all: draw every node nobody parents, which is what most
    // viewers do with a bare node list.
    const parented = new Set<number>();
    json.nodes?.forEach((node) => node.children?.forEach((child) => parented.add(child)));
    roots = (json.nodes ?? []).map((_, i) => i).filter((i) => !parented.has(i));
  }
  for (const root of roots) visit(root, identity());

  if (skippedPrimitives) {
    warnings.push(`${skippedPrimitives} non-triangle primitive${skippedPrimitives === 1 ? " was" : "s were"} skipped (only TRIANGLES are drawn).`);
  }
  if (!meshes.length) throw new GlbError("The file contains no triangle geometry to draw.");
  if (!meshes.some((m) => m.positions.length)) throw new GlbError("Every mesh in the file is empty.");

  return { meshes, bounds: { min, max }, warnings };
}
