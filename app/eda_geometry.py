"""Bounded SVG path geometry used by the EasyEDA library exporter.

Straight segments stay exact. Curves are flattened to a maximum chord error
of 0.005 EasyEDA units (0.00127 mm) for polygon/line primitives.
"""
from __future__ import annotations

import math
import re
import heapq
from dataclasses import dataclass

from errors import DownloadError

NUMBER = r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'


def number(value, default=None):
    if value == '' and default is not None:
        return default
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise DownloadError(f'EDA 坐标或尺寸无效：{value}') from exc
    if not math.isfinite(result) or abs(result) > 1e7:
        raise DownloadError('EDA 坐标或尺寸超出支持范围')
    return result


def points(text):
    tokens = re.findall(NUMBER, text)
    if re.sub(NUMBER, '', text).strip(' ,\t\r\n') or len(tokens) % 2:
        raise DownloadError('EDA 图元坐标列表无效')
    if len(tokens) > 400000:
        raise DownloadError('EDA 图元的顶点过多')
    return [(number(tokens[i]), number(tokens[i + 1])) for i in range(0, len(tokens), 2)]


def _distance(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if not length:
        return math.dist(point, start)
    t = max(0, min(1, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (length * length)))
    return math.dist(point, (start[0] + t * dx, start[1] + t * dy))


PAD_TOLERANCE = 0.005  # 0.00127 mm, the same bound as curve tessellation.


@dataclass(frozen=True)
class PadOutline:
    """A validated copper contour and its optional native Altium shape."""
    vertices: tuple
    shape: str | None
    center: tuple
    size: tuple
    rotation: float


def pad_outline(vertices, rotation=0, tolerance=PAD_TOLERANCE, check_cancelled=lambda: None):
    """Recognize geometry, rather than treating every POLYGON as a custom pad.

    A standard contour must match in both directions, including edge interiors;
    its bounding box alone never qualifies it. Tiny vendor steps may be within
    tolerance, but real notches, chamfers and holes must remain custom geometry.
    Coordinates here use EasyEDA's downward Y; rotation uses AD's CCW angle.
    """
    clean = []
    for vertex in vertices:
        check_cancelled()
        p = tuple(map(number, vertex))
        if not clean or math.dist(p, clean[-1]) > 1e-9:
            clean.append(p)
    if len(clean) > 1 and math.dist(clean[0], clean[-1]) <= 1e-9:
        clean.pop()
    if len(clean) < 3 or len(clean) > 2048:
        raise DownloadError('自定义焊盘轮廓缺少顶点或超过 2048 个顶点')
    # Work relative to a nearby point to avoid cancellation at large page origins.
    origin = clean[0]
    contour = [(p[0] - origin[0], p[1] - origin[1]) for p in clean]
    edges = list(zip(contour, contour[1:] + contour[:1]))

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def intersects(a, b, c, d):
        if (max(a[0], b[0]) < min(c[0], d[0]) - 1e-9 or
                max(c[0], d[0]) < min(a[0], b[0]) - 1e-9 or
                max(a[1], b[1]) < min(c[1], d[1]) - 1e-9 or
                max(c[1], d[1]) < min(a[1], b[1]) - 1e-9):
            return False
        ac, ad, ca, cb = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
        return ac * ad <= 0 and ca * cb <= 0

    for i, (a, b) in enumerate(edges):
        check_cancelled()
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            if intersects(a, b, *edges[j]):
                raise DownloadError('自定义焊盘轮廓自交或包含不支持的重复边')
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    if area <= 1e-12:
        raise DownloadError('自定义焊盘轮廓面积为零')

    def matches(points, template):
        # Segment midpoints catch chords across a notch or a curved boundary.
        def samples(p):
            return p + [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                        for a, b in zip(p, p[1:] + p[:1])]
        def directed(a, b):
            target = list(zip(b, b[1:] + b[:1]))
            return all(min(_distance(p, x, y) for x, y in target) <= tolerance for p in samples(a))
        return directed(points, template) and directed(template, points)

    longest = max(edges, key=lambda edge: math.dist(*edge))
    edge_angle = math.degrees(math.atan2(-(longest[1][1] - longest[0][1]),
                                        longest[1][0] - longest[0][0])) % 180
    angles = list(dict.fromkeys([rotation % 360, 0., edge_angle]))
    for angle in angles:
        check_cancelled()
        rad = math.radians(angle)
        c, s = math.cos(rad), math.sin(rad)
        local = [(c * x - s * y, -s * x - c * y) for x, y in contour]
        xmin, xmax = min(p[0] for p in local), max(p[0] for p in local)
        ymin, ymax = min(p[1] for p in local), max(p[1] for p in local)
        w, h = xmax - xmin, ymax - ymin
        if min(w, h) <= 2 * tolerance:
            continue
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        centered = [(x - cx, y - cy) for x, y in local]
        rectangle = [(-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)]
        kind = 'RECT' if matches(centered, rectangle) else None
        if kind is None:
            bevel = min(w, h) / 4
            octagon = [(-w/2 + bevel, -h/2), (w/2 - bevel, -h/2), (w/2, -h/2 + bevel),
                       (w/2, h/2 - bevel), (w/2 - bevel, h/2), (-w/2 + bevel, h/2),
                       (-w/2, h/2 - bevel), (-w/2, -h/2 + bevel)]
            if matches(centered, octagon):
                kind = 'OCTAGON'
        if kind is None and len(centered) >= 8:
            # Altium's Round pad with unequal dimensions is a capsule, not an
            # arbitrary ellipse. Match an adaptive capsule outline explicitly.
            radius = min(w, h) / 2
            steps = max(16, math.ceil(math.pi / math.acos(max(-1., 1 - tolerance / (4 * radius)))))
            if steps > 1024:
                continue  # Keep very large / complex geometry as a custom contour.
            shift = abs(w - h) / 2
            capsule = []
            for side in (0, 1):
                for i in range(steps + 1):
                    a = -math.pi / 2 + side * math.pi + i * math.pi / steps
                    u, v = radius * math.cos(a) + (shift if side == 0 else -shift), radius * math.sin(a)
                    capsule.append((u, v) if w >= h else (-v, u))
            if matches(centered, capsule):
                kind = 'OVAL'
        if kind:
            center = (origin[0] + c * cx - s * cy, origin[1] - s * cx - c * cy)
            return PadOutline(tuple(clean), kind, center, (w, h), angle)
    xmin, xmax = min(p[0] for p in clean), max(p[0] for p in clean)
    ymin, ymax = min(p[1] for p in clean), max(p[1] for p in clean)
    return PadOutline(tuple(clean), None, ((xmin + xmax)/2, (ymin + ymax)/2),
                      (xmax - xmin, ymax - ymin), rotation)


def polygon_anchor(vertices, preferred, tolerance=0.005, check_cancelled=lambda: None):
    """Find a circular electrical anchor wholly inside a custom copper polygon.

    Keep the vendor centre when possible. Concave polygons can have their
    bounding-box centre outside the copper; bounded cell subdivision finds an
    interior point without adding copper outside the original outline.
    """
    if len(vertices) < 3:
        raise DownloadError('自定义焊盘缺少多边形顶点')
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))

    def distance(p):
        inside = False
        for a, b in edges:
            if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
        d = min(_distance(p, a, b) for a, b in edges)
        return d if inside else -d

    clearance = distance(preferred)
    if clearance > tolerance:
        return preferred, clearance
    xmin, xmax = min(p[0] for p in vertices), max(p[0] for p in vertices)
    ymin, ymax = min(p[1] for p in vertices), max(p[1] for p in vertices)
    h = max(xmax - xmin, ymax - ymin) / 2
    if not h:
        raise DownloadError('自定义焊盘面积为零')
    best, best_d = preferred, clearance
    cells = []

    def cell(x, y, half):
        d = distance((x, y))
        heapq.heappush(cells, (-(d + half * math.sqrt(2)), x, y, half, d))

    cell((xmin + xmax) / 2, (ymin + ymax) / 2, h)
    count = 0
    while cells:
        check_cancelled()
        bound, x, y, half, d = heapq.heappop(cells)
        if d > best_d:
            best, best_d = (x, y), d
        if -bound - best_d <= tolerance:
            continue
        count += 1
        if count > 20000:
            raise DownloadError('自定义焊盘多边形过于复杂')
        half /= 2
        for dx, dy in ((-half, -half), (-half, half), (half, -half), (half, half)):
            cell(x + dx, y + dy, half)
    if best_d <= 0:
        raise DownloadError('自定义焊盘没有有效铜区域')
    return best, best_d


def _bezier(control, output, tolerance, depth=0):
    if max((_distance(p, control[0], control[-1]) for p in control[1:-1]), default=0) <= tolerance:
        output.append(control[-1])
        return
    if depth >= 20:
        raise DownloadError('EDA 曲线过于复杂')
    levels = [control]
    while len(levels[-1]) > 1:
        row = levels[-1]
        levels.append([((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(row, row[1:])])
    _bezier([row[0] for row in levels], output, tolerance, depth + 1)
    _bezier([row[-1] for row in reversed(levels)], output, tolerance, depth + 1)


def _arc(start, end, rx, ry, rotation, large, sweep, output, tolerance):
    if large not in (0, 1) or sweep not in (0, 1):
        raise DownloadError('EDA 圆弧标志无效')
    if start == end:
        return
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry:
        output.append(end)
        return
    phi = math.radians(rotation)
    cs, sn = math.cos(phi), math.sin(phi)
    dx, dy = (start[0] - end[0]) / 2, (start[1] - end[1]) / 2
    x, y = cs * dx + sn * dy, -sn * dx + cs * dy
    scale = x*x/(rx*rx) + y*y/(ry*ry)
    if scale > 1:
        rx *= math.sqrt(scale)
        ry *= math.sqrt(scale)
    ratio = max(0, (rx*rx*ry*ry - rx*rx*y*y - ry*ry*x*x) / (rx*rx*y*y + ry*ry*x*x))
    factor = math.sqrt(ratio) * (-1 if large == sweep else 1)
    ox, oy = factor * rx * y / ry, -factor * ry * x / rx
    center = (cs * ox - sn * oy + (start[0] + end[0]) / 2,
              sn * ox + cs * oy + (start[1] + end[1]) / 2)
    begin = math.atan2((y - oy) / ry, (x - ox) / rx)
    finish = math.atan2((-y - oy) / ry, (-x - ox) / rx)
    angle = (finish - begin) % (2 * math.pi)
    if not sweep:
        angle -= 2 * math.pi
    step = 2 * math.acos(max(-1, 1 - tolerance / max(rx, ry)))
    count = max(1, math.ceil(abs(angle) / max(step, 1e-6)))
    if count > 100000:
        raise DownloadError('EDA 圆弧的顶点过多')
    for i in range(1, count):
        a = begin + angle * i / count
        px, py = rx * math.cos(a), ry * math.sin(a)
        output.append((center[0] + cs * px - sn * py, center[1] + sn * px + cs * py))
    output.append(end)


def path_points(path, tolerance=0.005, check_cancelled=lambda: None):
    """Read M/L/H/V/C/S/Q/T/A/Z paths; return separate subpaths."""
    if not isinstance(path, str) or len(path) > 4000000:
        raise DownloadError('EDA 图形路径无效或过大')
    token_pattern = rf'[MmLlHhVvCcSsQqTtAaZz]|{NUMBER}'
    if re.sub(token_pattern, '', path).strip(' ,\t\r\n'):
        raise DownloadError('EDA 路径中包含不支持的命令')
    tokens = re.findall(token_pattern, path)
    cursor, command, previous = 0, '', ''
    current, control = (0., 0.), None
    subpaths, total_points = [], 0
    counts = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7}
    while cursor < len(tokens):
        check_cancelled()
        if tokens[cursor].isalpha():
            command = tokens[cursor]
            cursor += 1
        op, relative = command.upper(), command.islower()
        if op == 'Z':
            if not subpaths:
                raise DownloadError('EDA 闭合路径缺少起点')
            current = subpaths[-1][0]
            if subpaths[-1][-1] != current:
                subpaths[-1].append(current)
                total_points += 1
            command, previous, control = '', 'Z', None
            continue
        count = counts.get(op)
        if count is None or cursor + count > len(tokens):
            raise DownloadError('EDA 路径坐标不完整')
        values = [number(v) for v in tokens[cursor:cursor + count]]
        cursor += count
        def point(index):
            return (values[index] + (current[0] if relative else 0),
                    values[index + 1] + (current[1] if relative else 0))
        if op == 'M':
            current = point(0)
            subpaths.append([current])
            total_points += 1
            command = 'l' if relative else 'L'
        else:
            if not subpaths:
                raise DownloadError('EDA 路径必须以 M 开始')
            output = subpaths[-1]
            previous_size = len(output)
            if op in ('L', 'H', 'V'):
                end = point(0) if op == 'L' else ((values[0] + (current[0] if relative else 0), current[1]) if op == 'H'
                        else (current[0], values[0] + (current[1] if relative else 0)))
                output.append(end)
                control = None
            elif op in ('C', 'S', 'Q', 'T'):
                reflected = (2*current[0] - control[0], 2*current[1] - control[1]) if control and previous in (
                            ('C', 'S') if op == 'S' else ('Q', 'T')) else current
                if op == 'C': curve, end, control = [current, point(0), point(2), point(4)], point(4), point(2)
                elif op == 'S': curve, end, control = [current, reflected, point(0), point(2)], point(2), point(0)
                elif op == 'Q': curve, end, control = [current, point(0), point(2)], point(2), point(0)
                else: curve, end, control = [current, reflected, point(0)], point(0), reflected
                _bezier(curve, output, tolerance)
            else:
                end = point(5)
                _arc(current, end, *values[:5], output, tolerance)
                control = None
            current = end
            total_points += len(output) - previous_size
        previous = op
        if total_points > 200000:
            raise DownloadError('EDA 路径的顶点过多')
    return subpaths
