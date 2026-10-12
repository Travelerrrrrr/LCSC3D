"""Source placement anchors and rotation helpers; no CAD runtime or mesh objects."""
from dataclasses import dataclass
import hashlib
import math
import re

from errors import DownloadError


@dataclass(frozen=True)
class ModelOrigin:
    position: tuple[float, float, float]
    sha256: str


def obj_origin(raw, check=lambda: None):
    """Scan only vertex lines for Standard's XY-centre / minimum-Z anchor.

    No face/material parsing, triangulation, vertex list or STEP comparison.
    Apart from the caller's text, memory is bounded by one line/hash chunk.
    Coordinates use the official asset's millimetre basis, just like its STEP.
    """
    check()
    if not isinstance(raw, str) or not raw or len(raw) > 64*1024*1024:
        raise DownloadError('官方模型放置基准不可用，已跳过关联')
    low, high, count = [math.inf]*3, [-math.inf]*3, 0
    for match in re.finditer(r'(?m)^[ \t]*v[ \t]+([^\r\n]*)', raw):
        if count % 1024 == 0:
            check()
        fields = match[1].partition('#')[0].split(maxsplit=3)
        try:
            if len(fields) < 3:
                raise ValueError()
            point = tuple(float(v) for v in fields[:3])
            if not all(math.isfinite(v) and abs(v) <= 1e9 for v in point):
                raise ValueError()
        except ValueError as exc:
            raise DownloadError('官方模型放置基准含无效顶点，已跳过关联') from exc
        for i, value in enumerate(point):
            low[i], high[i] = min(low[i], value), max(high[i], value)
        count += 1
        if count > 1_000_000:
            raise DownloadError('官方模型顶点过多，无法获取放置基准')
    if count < 2 or low == high:
        raise DownloadError('官方模型缺少有效放置基准，已跳过关联')
    digest = hashlib.sha256()
    for start in range(0, len(raw), 65536):
        check()
        digest.update(raw[start:start+65536].encode('utf-8'))
    check()
    return ModelOrigin(((low[0]+high[0])/2, (low[1]+high[1])/2, low[2]), digest.hexdigest())


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
