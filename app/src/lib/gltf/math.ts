// Column-major 4x4 matrices, the layout glTF stores and WebGL uploads. Every
// function here takes and returns `Float32Array(16)` (or a 3-vector), and
// nothing in the viewer does matrix arithmetic anywhere else.

export type Vec3 = [number, number, number];

export function identity(): Float32Array {
  const m = new Float32Array(16);
  m[0] = m[5] = m[10] = m[15] = 1;
  return m;
}

/** `a * b` (apply `b` first, then `a`), both column-major. */
export function multiply(a: Float32Array, b: Float32Array): Float32Array {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col += 1) {
    for (let row = 0; row < 4; row += 1) {
      let sum = 0;
      for (let k = 0; k < 4; k += 1) sum += a[k * 4 + row] * b[col * 4 + k];
      out[col * 4 + row] = sum;
    }
  }
  return out;
}

/** The matrix for a glTF node's `translation`/`rotation`/`scale` (T * R * S). */
export function fromTRS(
  translation: Vec3 = [0, 0, 0],
  rotation: [number, number, number, number] = [0, 0, 0, 1],
  scale: Vec3 = [1, 1, 1]
): Float32Array {
  const [x, y, z, w] = rotation;
  const xx = x * x, yy = y * y, zz = z * z;
  const xy = x * y, xz = x * z, yz = y * z;
  const wx = w * x, wy = w * y, wz = w * z;
  const [sx, sy, sz] = scale;
  const m = new Float32Array(16);
  m[0] = (1 - 2 * (yy + zz)) * sx;
  m[1] = 2 * (xy + wz) * sx;
  m[2] = 2 * (xz - wy) * sx;
  m[4] = 2 * (xy - wz) * sy;
  m[5] = (1 - 2 * (xx + zz)) * sy;
  m[6] = 2 * (yz + wx) * sy;
  m[8] = 2 * (xz + wy) * sz;
  m[9] = 2 * (yz - wx) * sz;
  m[10] = (1 - 2 * (xx + yy)) * sz;
  m[12] = translation[0];
  m[13] = translation[1];
  m[14] = translation[2];
  m[15] = 1;
  return m;
}

export function transformPoint(m: Float32Array, p: Vec3): Vec3 {
  const [x, y, z] = p;
  const w = m[3] * x + m[7] * y + m[11] * z + m[15] || 1;
  return [
    (m[0] * x + m[4] * y + m[8] * z + m[12]) / w,
    (m[1] * x + m[5] * y + m[9] * z + m[13]) / w,
    (m[2] * x + m[6] * y + m[10] * z + m[14]) / w,
  ];
}

export function perspective(fovY: number, aspect: number, near: number, far: number): Float32Array {
  const f = 1 / Math.tan(fovY / 2);
  const m = new Float32Array(16);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (far + near) / (near - far);
  m[11] = -1;
  m[14] = (2 * far * near) / (near - far);
  return m;
}

export function lookAt(eye: Vec3, target: Vec3, up: Vec3): Float32Array {
  let zx = eye[0] - target[0], zy = eye[1] - target[1], zz = eye[2] - target[2];
  let len = Math.hypot(zx, zy, zz) || 1;
  zx /= len; zy /= len; zz /= len;
  let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
  len = Math.hypot(xx, xy, xz) || 1;
  xx /= len; xy /= len; xz /= len;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  const m = new Float32Array(16);
  m[0] = xx; m[1] = yx; m[2] = zx;
  m[4] = xy; m[5] = yy; m[6] = zy;
  m[8] = xz; m[9] = yz; m[10] = zz;
  m[12] = -(xx * eye[0] + xy * eye[1] + xz * eye[2]);
  m[13] = -(yx * eye[0] + yy * eye[1] + yz * eye[2]);
  m[14] = -(zx * eye[0] + zy * eye[1] + zz * eye[2]);
  m[15] = 1;
  return m;
}

/**
 * The 3x3 inverse-transpose of `m`'s upper-left block, for transforming
 * normals under a non-uniform scale. Falls back to the plain block when the
 * matrix is singular (a zero scale), which draws that mesh unlit rather than
 * with NaN normals.
 */
export function normalMatrix(m: Float32Array): Float32Array {
  const a00 = m[0], a01 = m[1], a02 = m[2];
  const a10 = m[4], a11 = m[5], a12 = m[6];
  const a20 = m[8], a21 = m[9], a22 = m[10];
  const b01 = a22 * a11 - a12 * a21;
  const b11 = -a22 * a10 + a12 * a20;
  const b21 = a21 * a10 - a11 * a20;
  const det = a00 * b01 + a01 * b11 + a02 * b21;
  if (!det) return new Float32Array([a00, a01, a02, a10, a11, a12, a20, a21, a22]);
  const inv = 1 / det;
  // Inverse, then transposed: element (r, c) of the inverse goes to (c, r).
  return new Float32Array([
    b01 * inv, b11 * inv, b21 * inv,
    (-a22 * a01 + a02 * a21) * inv, (a22 * a00 - a02 * a20) * inv, (-a21 * a00 + a01 * a20) * inv,
    (a12 * a01 - a02 * a11) * inv, (-a12 * a00 + a02 * a10) * inv, (a11 * a00 - a01 * a10) * inv,
  ]);
}
