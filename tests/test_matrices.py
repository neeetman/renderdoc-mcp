import math
import struct

from renderdoc_mcp import matrices as mx


def f32(m):
    """Round to float32, as matrices come out of a constant buffer."""
    return [list(struct.unpack("<4f", struct.pack("<4f", *r))) for r in m]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def ue_proj(fov_x_deg=75.0, aspect=16 / 9, near=4.0, jitter=(0.0, 0.0)):
    """UE-style reversed-Z infinite-far perspective (row-vector)."""
    px = 1 / math.tan(math.radians(fov_x_deg) / 2)
    py = px * aspect
    return [[px, 0, 0, 0], [0, py, 0, 0], [jitter[0], jitter[1], 0, 1], [0, 0, near, 0]]


def view(yaw_deg=97.6, pitch_deg=-14.4, pos=(0.0, 0.0, 0.0)):
    """Row-vector world->view: columns are right, up, forward."""
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    f = [math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)]
    r = [-math.sin(y), math.cos(y), 0.0]
    u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
    M = [[r[i], u[i], f[i], 0.0] for i in range(3)]
    t = [-sum(pos[i] * c[i] for i in range(3)) for c in (r, u, f)]
    M.append(t + [1.0])
    return M, f


def pack(*ms):
    return b"".join(struct.pack("<16f", *[x for r in m for x in r]) for m in ms)


def test_perspective_reversed_infinite():
    info = mx.classify(ue_proj(jitter=(9.7e-5, 1.28e-3)))
    assert info["kind"] == "perspective"
    assert abs(info["fov_x_deg"] - 75) < 1e-3
    assert abs(info["aspect"] - 16 / 9) < 1e-4
    assert info["depth"] == "reversed-Z infinite far" and abs(info["near"] - 4) < 1e-6
    assert "jitter" in mx.describe(info)


def test_standard_z_perspective_near_far():
    n, f = 0.1, 1000.0
    c = f / (f - n)
    d = -n * f / (f - n)
    info = mx.classify([[1, 0, 0, 0], [0, 1.5, 0, 0], [0, 0, c, 1], [0, 0, d, 0]])
    assert info["depth"] == "standard-Z"
    assert abs(info["near"] - n) < 1e-6 and abs(info["far"] - f) < 1e-3


def test_world_to_clip_camera_relative_and_absolute():
    V, fwd = view()
    info = mx.classify(mul(V, ue_proj()))
    assert info["kind"] == "world_to_clip" and info["camera_relative"]
    assert all(abs(a - b) < 1e-5 for a, b in zip(info["camera_forward"], fwd))
    assert abs(info["fov_x_deg"] - 75) < 1e-3

    V2, _ = view(pos=(42484.0, 3737.0, 900.0))
    info2 = mx.classify(mul(V2, ue_proj()))
    assert info2["kind"] == "world_to_clip" and not info2["camera_relative"]


def test_inverses_are_labelled():
    assert mx.classify(f32(mx.inverse4(ue_proj())))["kind"] == "inverse_perspective"
    V, _ = view(pos=(100.0, 200.0, 300.0))
    assert mx.classify(f32(mx.inverse4(mul(V, ue_proj()))))["kind"] == "inverse_world_to_clip"


def test_view_rotation_is_rigid():
    V, _ = view()
    assert mx.classify(V)["kind"] == "rigid"


def test_integer_data_rejected():
    ints = struct.unpack("<16f", struct.pack("<16i", *range(1, 17)))
    assert mx.classify([list(ints[i * 4:i * 4 + 4]) for i in range(4)]) is None


def test_scan_finds_aligned_matrices_and_skips_straddling_windows():
    V, _ = view()
    P = ue_proj()
    data = pack(mul(V, P), V, P) + struct.pack("<4f", 1460, 821, 1 / 1460, 1 / 821)
    found = mx.scan(data)
    kinds = {f["offset"]: f["kind"] for f in found}
    assert kinds.get(0) == "world_to_clip"
    assert kinds.get(64) == "rigid"
    assert kinds.get(128) == "perspective"
    # windows straddling two matrices (offsets 16..48, 80..112) must not be reported as cameras
    assert not any(o % 64 and k in ("world_to_clip", "perspective") for o, k in kinds.items())


def test_duplicates_merged():
    P = ue_proj()
    found = mx.scan(pack(P, P), kinds={"perspective"})
    assert len(found) == 1 and found[0]["also_at"] == [64]
