"""Small matrix helpers for source and target rotation conventions; no CAD runtime."""
import math


def rotation_zxy(angles):
    x, y, z = (math.radians(float(a)) for a in angles)
    cx, cy, cz, sx, sy, sz = math.cos(x), math.cos(y), math.cos(z), math.sin(x), math.sin(y), math.sin(z)
    return ((cy*cz + sy*sx*sz, -cy*sz + sy*sx*cz, sy*cx),
            (cx*sz, cx*cz, -sx),
            (-sy*cz + cy*sx*sz, sy*sz + cy*sx*cz, cy*cx))


def euler_xyz(matrix):
    """Decompose Rz(z) Ry(y) Rx(x); return right-handed X/Y/Z degrees."""
    r = matrix
    y = math.asin(max(-1., min(1., -float(r[2][0]))))
    if abs(math.cos(y)) > 1e-9:
        x, z = math.atan2(float(r[2][1]), float(r[2][2])), math.atan2(float(r[1][0]), float(r[0][0]))
    else:
        x, z = math.atan2(-float(r[1][2]), float(r[1][1])), 0.
    return tuple(math.degrees(v) for v in (x, y, z))
