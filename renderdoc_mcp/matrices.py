"""Find and classify 4x4 camera matrices in raw constant-buffer bytes.

Pure Python (no renderdoc import) so it can be unit-tested. Works when shader reflection names
were stripped (typical for shipping builds): matrices are recognised by their numeric structure.

Conventions: D3D row-vector maths (clip = v * M) as stored; every check also runs on the
transpose, and a matrix whose inverse is a projection is reported as "INVERSE of ...".
"""
import math
import struct


def inverse4(m):
    """Gauss-Jordan inverse of a 4x4 list-of-rows; None if singular."""
    a = [list(map(float, r)) + [1.0 if i == j else 0.0 for j in range(4)] for i, r in enumerate(m)]
    for c in range(4):
        p = max(range(c, 4), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        piv = a[c][c]
        a[c] = [x / piv for x in a[c]]
        for r in range(4):
            if r != c and a[r][c] != 0:
                f = a[r][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [r[4:] for r in a]


def _t(m):
    return [list(c) for c in zip(*m)]


def _perspective(M, layout):
    # D3D row-vector perspective: M[2][3] = +-1, M[3][3] = 0, x/y on the diagonal
    if not (abs(abs(M[2][3]) - 1) < 1e-4 and abs(M[3][3]) < 1e-6 and M[0][0] != 0 and M[1][1] != 0
            and abs(M[0][1]) < 1e-6 and abs(M[1][0]) < 1e-6 and abs(M[0][3]) < 1e-6 and abs(M[1][3]) < 1e-6):
        return None
    a, b, c, d = M[0][0], M[1][1], M[2][2], M[3][2]
    out = {"kind": "perspective", "layout": layout,
           "fov_y_deg": math.degrees(2 * math.atan(1 / abs(b))),
           "fov_x_deg": math.degrees(2 * math.atan(1 / abs(a))),
           "aspect": abs(b / a), "jitter": [M[2][0], M[2][1]]}
    if abs(c) < 1e-6 and d != 0:
        out.update(depth="reversed-Z infinite far", near=abs(d), far=math.inf)
    elif c not in (0, 1):
        z0, z1 = -d / c, d / (1 - c)          # view z where NDC z = 0 and = 1
        out.update(depth="reversed-Z" if abs(z0) > abs(z1) else "standard-Z",
                   near=min(abs(z0), abs(z1)), far=max(abs(z0), abs(z1)))
    return out


def _ortho_or_affine(M, layout):
    if abs(M[0][3]) > 1e-5 or abs(M[1][3]) > 1e-5 or abs(M[2][3]) > 1e-5 or abs(M[3][3] - 1) > 1e-5:
        return None
    R = [r[:3] for r in M[:3]]
    norms = [math.sqrt(sum(x * x for x in r)) for r in R]
    t = M[3][:3]
    if all(abs(n - 1) < 1e-3 for n in norms):
        return {"kind": "rigid", "layout": layout, "translation": t}
    diag = all(abs(R[i][j]) < 1e-6 for i in range(3) for j in range(3) if i != j)
    if diag and all(R[i][i] != 0 for i in range(3)):
        return {"kind": "ortho/scale", "layout": layout, "scale": [R[0][0], R[1][1], R[2][2]],
                "translation": t}
    return None


def _world_to_clip(M, layout):
    # clip = v * (view * proj): col3 = camera forward (unit), col0 = right*Px, col1 = up*Py,
    # all mutually orthogonal
    cols = [[M[i][j] for i in range(3)] for j in range(4)]
    n = [math.sqrt(sum(x * x for x in c)) for c in cols]
    if abs(n[3] - 1) > 2e-3 or n[0] < 1e-3 or n[1] < 1e-3:
        return None
    # every input axis must reach clip space; windows straddling two matrices have a zero row
    if any(all(abs(M[i][j]) < 1e-9 for j in range(3)) for i in range(3)):
        return None
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    if (abs(dot(cols[0], cols[3])) / n[0] > 2e-3 or abs(dot(cols[1], cols[3])) / n[1] > 2e-3
            or abs(dot(cols[0], cols[1])) / (n[0] * n[1]) > 2e-3):
        return None
    w_t = M[3][3]
    return {"kind": "world_to_clip", "layout": layout,
            "fov_y_deg": math.degrees(2 * math.atan(1 / n[1])),
            "fov_x_deg": math.degrees(2 * math.atan(1 / n[0])),
            "aspect": n[1] / n[0], "camera_forward": cols[3],
            "camera_relative": abs(w_t) < 1e-3, "w_translation": w_t}


def _classify_one(m):
    for layout, M in (("rows", m), ("transposed", _t(m))):
        r = _perspective(M, layout) or _ortho_or_affine(M, layout)
        if r:
            return r
    for layout, M in (("rows", m), ("transposed", _t(m))):
        r = _world_to_clip(M, layout)
        if r:
            return r
    return None


def plausible(m):
    flat = [x for r in m for x in r]
    if any(math.isnan(x) or math.isinf(x) or abs(x) > 1e7 for x in flat):
        return False
    # integer / packed data reads as denormals or absurdly small floats (ints up to ~2^26 land
    # below 1e-20); real float32 matrices keep round-off well above that
    if any(x != 0.0 and abs(x) < 1e-20 for x in flat):
        return False
    return sum(1 for x in flat if x != 0.0) >= 4


def classify(m):
    """Classify a 4x4 list-of-rows. Returns a dict or None."""
    if not plausible(m):
        return None
    r = _classify_one(m)
    if r and r["kind"] in ("perspective", "world_to_clip") and r["layout"] == "rows":
        return r
    inv = inverse4(m)
    if inv and plausible(inv):
        ri = _classify_one(inv)
        if ri and ri["kind"] in ("perspective", "world_to_clip") and ri["layout"] == "rows":
            ri = dict(ri)
            ri["kind"] = "inverse_" + ri["kind"]
            return ri
    return r


def scan(data, names=None, kinds=None):
    """Slide a 64-byte window over `data` at 16-byte steps; return distinct matrices.

    names: optional {byte_offset: variable name} from reflection.
    kinds: optional set of kinds to keep.
    """
    names = names or {}
    nfl = len(data) // 4
    fl = struct.unpack(f"<{nfl}f", data[:nfl * 4])
    seen = {}
    for off in range(0, nfl - 15, 4):
        m = [list(fl[off + r * 4: off + r * 4 + 4]) for r in range(4)]
        info = classify(m)
        if not info or (kinds and info["kind"] not in kinds):
            continue
        key = tuple(round(x, 5) for x in fl[off:off + 16])
        if key in seen:
            seen[key]["also_at"].append(off * 4)
            continue
        info = dict(info)
        info.update(offset=off * 4, name=names.get(off * 4, ""), also_at=[],
                    matrix=[[round(x, 7) for x in r] for r in m])
        seen[key] = info
    return list(seen.values())


def describe(info):
    """One-line human/agent readable summary of a classify() result."""
    k = info["kind"]
    base = k.replace("inverse_", "")
    labels = {"perspective": "PERSPECTIVE", "world_to_clip": "WORLD->CLIP", "rigid": "RIGID",
              "ortho/scale": "ORTHO/SCALE"}
    s = ("INVERSE of " if k.startswith("inverse_") else "") + labels[base]
    if base in ("perspective", "world_to_clip"):
        s += f" fovY={info['fov_y_deg']:.2f} fovX={info['fov_x_deg']:.2f} aspect={info['aspect']:.4f}"
    if base == "perspective":
        if "depth" in info:
            s += f" {info['depth']} near={info['near']:.4g}" + ("" if info["far"] == math.inf else f" far={info['far']:.4g}")
        if any(abs(j) > 0 for j in info["jitter"]):
            s += f" jitter=({info['jitter'][0]:.3g},{info['jitter'][1]:.3g})"
    if base == "world_to_clip":
        f = info["camera_forward"]
        s += f" forward=({f[0]:.4f},{f[1]:.4f},{f[2]:.4f}) " + \
             ("camera-relative" if info["camera_relative"] else f"absolute (w_t={info['w_translation']:.6g})")
    if base == "rigid":
        s += " t=({:.6g},{:.6g},{:.6g})".format(*info["translation"])
    if base == "ortho/scale":
        s = "ORTHO/SCALE s=({:.4g},{:.4g},{:.4g})".format(*info["scale"]) + " t=({:.4g},{:.4g},{:.4g})".format(*info["translation"])
    return s
