"""
Prototype of the Houdini COPs "per-cell aligned grain" shader.

Same math that goes into the VEX snippet (stone_wall_align.vfl):
  1. jittered-grid Voronoi -> per-pixel cell id, F1, F2
  2. mortar mask from (F2 - F1)
  3. per-cell random frame (angle) -> rotate detail UVs per stone
  4. stretched value-noise grain in the cell frame
  5. per-cell albedo variation + mortar + edge AO

Outputs:
  preview.png       - the shaded stone wall (visual target for Houdini)
  preview_cells.png - cell id map + per-cell orientation ticks
                      (the "alignment points overlaid" figure for the breakdown)
"""
import numpy as np
from scipy.spatial import cKDTree
from PIL import Image

# ---------------- params (mirror the VEX ch() parms) ----------------
RES = 1024
CELLS_X, CELLS_Y = 9, 9
JITTER = 0.8
MORTAR_W = 0.055      # in units of cell size
DETAIL_FREQ = 26.0
GRAIN_STRETCH = 7.0
GRAIN_STRENGTH = 0.45
SEED = 7

rng = np.random.default_rng(SEED)

# ---------------- feature points (jittered grid) ----------------
gx = (np.arange(CELLS_X) + 0.5) / CELLS_X
gy = (np.arange(CELLS_Y) + 0.5) / CELLS_Y
PX, PY = np.meshgrid(gx, gy)
pts = np.stack([PX.ravel(), PY.ravel()], axis=1)
pts += (rng.random(pts.shape) - 0.5) * (JITTER / CELLS_X)
pts = np.clip(pts, 0, 1)

# ---------------- per-pixel F1 / F2 / cell id ----------------
yy, xx = np.mgrid[0:RES, 0:RES]
uv = np.stack([xx / (RES - 1), yy / (RES - 1)], axis=-1)          # (RES,RES,2)
d, idx = cKDTree(pts).query(uv.reshape(-1, 2), k=2)
f1 = d[:, 0].reshape(RES, RES)
f2 = d[:, 1].reshape(RES, RES)
cell = idx[:, 0].reshape(RES, RES)
cell_size = 1.0 / CELLS_X

# ---------------- value noise ----------------
def _hash2(x, y):
    h = (x.astype(np.uint64) * np.uint64(374761393) +
         y.astype(np.uint64) * np.uint64(668265263))
    h = (h ^ (h >> 13)) * np.uint64(1274126177)
    h ^= h >> 16
    return (h & np.uint64(0xFFFFFFFF)).astype(np.float64) / 4294967295.0

def vnoise(x, y):
    xi, yi = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    xf, yf = x - xi, y - yi
    u, v = xf * xf * (3 - 2 * xf), yf * yf * (3 - 2 * yf)
    a = _hash2(xi, yi); b = _hash2(xi + 1, yi)
    c = _hash2(xi, yi + 1); d_ = _hash2(xi + 1, yi + 1)
    return a + (b - a) * u + (c - a) * v + (a - b - c + d_) * u * v

def fbm(x, y, octaves=4):
    amp, tot, norm = 0.5, np.zeros_like(x), 0.0
    for _ in range(octaves):
        tot += amp * vnoise(x, y)
        norm += amp
        x, y, amp = x * 2.03 + 11.3, y * 2.03 + 7.7, amp * 0.5
    return tot / norm

# ---------------- per-cell frame ----------------
cell_hash = _hash2((cell % 4096).astype(np.int64),
                   (cell // 4096).astype(np.int64) + 100)
angle = cell_hash * 2 * np.pi                                # random frame per stone
ca, sa = np.cos(angle), np.sin(angle)

cx = pts[cell.ravel(), 0].reshape(RES, RES)                 # feature point = pivot
cy = pts[cell.ravel(), 1].reshape(RES, RES)
rx, ry = uv[..., 0] - cx, uv[..., 1] - cy
qx = (rx * ca + ry * sa) * DETAIL_FREQ                       # rotate detail UVs
qy = (-rx * sa + ry * ca) * DETAIL_FREQ

grain = fbm(qx * 1.0, qy * GRAIN_STRETCH)                   # stretched along cell x
tooth = fbm(uv[..., 0] * 220, uv[..., 1] * 220, octaves=2)  # fine surface tooth

# ---------------- shading ----------------
mortar = np.clip((f2 - f1) / (MORTAR_W * cell_size), 0, 1)
mortar = mortar * mortar * (3 - 2 * mortar)                 # smoothstep
ao = np.clip((f2 - f1) / (MORTAR_W * 2.5 * cell_size), 0, 1)
ao = np.sqrt(ao)                                            # bevel-ish falloff

tone = _hash2((cell % 4096).astype(np.int64),
              (cell // 4096).astype(np.int64) + 777)
stone_lo = np.array([0.42, 0.38, 0.34])
stone_hi = np.array([0.62, 0.58, 0.53])
base = stone_lo[None, None, :] + (stone_hi - stone_lo)[None, None, :] * tone[..., None]

shade = (1 - GRAIN_STRENGTH * 0.5) + GRAIN_STRENGTH * (grain - 0.5)
shade = shade * (0.92 + 0.16 * tooth)
stone_col = np.clip(base * shade[..., None] * (0.55 + 0.45 * ao[..., None]), 0, 1)
mortar_col = np.array([0.16, 0.15, 0.14]) * (0.8 + 0.4 * tooth[..., None])

col = mortar_col * (1 - mortar[..., None]) + stone_col * mortar[..., None]
Image.fromarray((np.clip(col, 0, 1) * 255).astype(np.uint8)).save("preview.png")

# ---------------- cell / orientation overlay figure ----------------
hue = (cell_hash * 360).astype(np.int32)
sat = np.full_like(hue, 45, dtype=np.int32)
val = np.full_like(hue, 90, dtype=np.int32)
import colorsys
flat = np.zeros((RES, RES, 3), np.uint8)
for h in np.unique(hue):
    m = hue == h
    r, g, b = colorsys.hsv_to_rgb(h / 360, 0.45, 0.9)
    flat[m] = (np.array([r, g, b]) * 255).astype(np.uint8)
img = Image.fromarray(flat).convert("RGB")
from PIL import ImageDraw
dr = ImageDraw.Draw(img)
step = RES // CELLS_X
L = step * 0.32
# angle per cell: same hash as the shading pass, evaluated on the cell grid order
order = np.arange(len(pts)).reshape(CELLS_Y, CELLS_X)
for j in range(CELLS_Y):
    for i in range(CELLS_X):
        k = order[j, i]
        a = float(_hash2(np.array([k % 4096]), np.array([k // 4096 + 100]))[0]) * 2 * np.pi
        px_, py_ = pts[k] * RES
        dx, dy = np.cos(a) * L, np.sin(a) * L
        dr.line([px_ - dx, py_ - dy, px_ + dx, py_ + dy], fill=(20, 20, 20), width=3)
        dr.ellipse([px_ - 4, py_ - 4, px_ + 4, py_ + 4], fill=(20, 20, 20))
img.save("preview_cells.png")
print("wrote preview.png and preview_cells.png")
