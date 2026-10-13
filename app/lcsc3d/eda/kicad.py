"""KiCad 8+ native library target for EDA IR 1.x.

No KiCad installation is needed at runtime. Curves without a native equivalent
are flattened at 0.00127 mm tolerance. Critical unsupported geometry is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import base64
from decimal import ROUND_HALF_UP
import hashlib
from lcsc3d.models.model_cache import cached_model, reuse_models
import json
import math
import re
from pathlib import PurePosixPath

from lcsc3d.eda.eda_ir import (D, Point, Pin, Pad, Via, Graphic, Component, Extension, decimal, xy, rotate,
                    require_domain, check_extensions, graphic_paths)
from lcsc3d.eda.eda_geometry import polygon_anchor
from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.eda_targets import KICAD
from lcsc3d.core.app_logging import traced, safe_part, record_error

FORMATS = ('KICAD_SYMBOL', 'KICAD_FOOTPRINT')
FORMAT_LABELS = {'KICAD_SYMBOL': 'KiCad 符号库', 'KICAD_FOOTPRINT': 'KiCad 封装库'}
ELECTRICAL_TYPES = (
    ('未指定', 'unspecified'), ('输入', 'input'), ('输出', 'output'),
    ('双向', 'bidirectional'), ('三态', 'tri_state'), ('无源', 'passive'),
    ('自由', 'free'), ('电源输入', 'power_in'), ('电源输出', 'power_out'),
    ('开集电极', 'open_collector'), ('开射极', 'open_emitter'), ('不连接', 'no_connect'))
ELECTRICAL_POLICIES = frozenset({'source', *(value for _, value in ELECTRICAL_TYPES)})


def pin_electrical_type(value, policy='source'):
    """Apply target policy without changing IR; unsupported values are unspecified."""
    selected = value if policy == 'source' else policy
    return selected if selected in ELECTRICAL_POLICIES - {'source'} else 'unspecified'

LAYERS = {'top_copper': 'F.Cu', 'bottom_copper': 'B.Cu', 'top_silk': 'F.SilkS',
          'bottom_silk': 'B.SilkS', 'top_mask': 'F.Mask', 'bottom_mask': 'B.Mask',
          'top_paste': 'F.Paste', 'bottom_paste': 'B.Paste', 'outline': 'Edge.Cuts',
          'documentation': 'Dwgs.User', 'mechanical': 'Dwgs.User', 'component_shape': 'Dwgs.User',
          'top_assembly': 'F.Fab', 'bottom_assembly': 'B.Fab',
          'top_component_shape': 'F.Fab', 'top_component_mark': 'F.Fab',
          **{f'inner_copper_{n}': f'In{n}.Cu' for n in range(1, 31)}}
SYMBOL_VERSION = 20211014
FOOTPRINT_VERSION = 20240108
TOLERANCE = 0.00127
BODY_LAYERS = frozenset({'component_shape', 'top_component_shape', 'top_component_mark'})


def n(value):
    result = decimal(value).quantize(D('0.000001'), rounding=ROUND_HALF_UP)
    return '0' if not result else format(result, 'f').rstrip('0').rstrip('.')


def model_number(value):
    """Model scale is dimensionless: preserve small positive values exactly."""
    result = decimal(value)
    return '0' if not result else format(result.normalize(),'f')


def q(value):
    text = str(value)
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise DownloadError('KiCad 文字含有不支持的控制字符')
    return json.dumps(text, ensure_ascii=False)


def identifier(value, fallback):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')
    if not value:
        value = fallback
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])', value.split('.')[0], re.I):
        value = '_' + value
    if len(value) > 100:
        value = value[:80] + '_' + hashlib.sha256(value.encode()).hexdigest()[:12]
    return value


def library_name(value):
    value = str(value).strip()
    if not value or identifier(value, '') != value or len(value) > 80:
        raise DownloadError('请填写有效的 KiCad 库名称（最多 80 字符，不含路径和冒号）')
    return value


def p(point, *, pcb=False):
    return n(point.x) + ' ' + n(-point.y if pcb else point.y)


def effects(style, *, hide=False, justify='middle', pcb=False, mirrored=False, pin=False):
    check_extensions(style)
    if style.height <= 0:
        raise DownloadError('KiCad 文字高度必须大于零')
    # KiCad's stroke-font height is not a TrueType point size. Keep native pin
    # labels at most 50 mil to fit the common 100 mil source pin pitch.
    # Pin effects do not support the text 'hide' flag in KiCad. A zero native
    # text size hides an individual label while preserving its electrical name.
    size = '0' if pin and hide else n(min(style.height, D('1.27')) if pin else style.height)
    values = ('(justify ' + ' '.join(filter(None, ({'start': 'left', 'end': 'right'}.get(justify, ''),
                                                  'mirror' if mirrored else ''))) + ')') if justify != 'middle' or mirrored else ''
    return f'(effects (font (size {size} {size})) {values} {"hide" if hide and not pin else ""})'


def ellipse(center, radii):
    rx, ry = xy(radii)
    if min(rx, ry) <= 0:
        raise DownloadError('KiCad 圆或椭圆半径必须大于零')
    count = max(16, math.ceil(math.pi / math.acos(max(-1., 1 - TOLERANCE / max(rx, ry)))))
    if count > 200000:
        raise DownloadError('KiCad 曲线过大')
    cx, cy = xy(center)
    return [Point(decimal(cx + rx * math.cos(i * 2 * math.pi / count)),
                  decimal(cy + ry * math.sin(i * 2 * math.pi / count))) for i in range(count)]


def contours(item, check):
    if item.kind in ('path', 'region'):
        return [[Point(decimal(x), decimal(y)) for x, y in path] for path in graphic_paths(item, check)]
    if item.kind in ('polyline', 'polygon'):
        return [list(item.points)]
    if item.kind == 'rect':
        pos, size = item.points[0], item.size
        if min(size.x, size.y) <= 0:
            raise DownloadError('KiCad 矩形尺寸无效')
        # Rectangles use square corners in this target; keep source radii in IR.
        return [[pos, Point(pos.x + size.x, pos.y), Point(pos.x + size.x, pos.y + size.y),
                 Point(pos.x, pos.y + size.y), pos]]
    if item.kind == 'ellipse':
        vertices = ellipse(item.points[0], item.radius)
        return [vertices + vertices[:1]]
    raise DownloadError('KiCad 导出暂不支持图元 ' + item.kind)


def symbol_graphic(item, check):
    check_extensions(item)
    if not item.visible:
        return []
    if item.kind == 'text':
        return [f'(text {q(item.text)} (at {p(item.points[0])} {n(item.rotation * 10)}) '
                f'{effects(item.text_style, justify=item.justify)})']
    style = {'solid': 'default', 'dash': 'dash', 'dot': 'dot', 'dash_dot': 'dash_dot'}.get(item.stroke.style)
    if style is None or item.stroke.width < 0:
        raise DownloadError('KiCad 符号线型或线宽不受支持')
    stroke = f'(stroke (width {n(item.stroke.width)}) (type {style}))'
    fill = 'none'
    if item.kind == 'rect':
        fill = 'background'
    elif item.fill != 'none':
        color = item.fill.lstrip('#')
        if len(color) == 3:
            color = ''.join(c * 2 for c in color)
        try:
            fill = 'background' if sum(bytes.fromhex(color)) / 3 >= 192 else 'outline'
        except ValueError as exc:
            raise DownloadError('KiCad 符号填充颜色无效') from exc
    if item.kind == 'ellipse' and item.radius.x == item.radius.y:
        return [f'(circle (center {p(item.points[0])}) (radius {n(item.radius.x)}) {stroke} (fill (type {fill})))']
    if item.kind == 'rect':
        start = item.points[0]
        end = Point(start.x + item.size.x, start.y + item.size.y)
        return [f'(rectangle (start {p(start)}) (end {p(end)}) {stroke} (fill (type {fill})))']
    result = []
    for path in contours(item, check):
        if len(path) < 2:
            raise DownloadError('KiCad 符号线段缺少顶点')
        if item.kind == 'polygon' or fill != 'none':
            if path[-1] != path[0]:
                path.append(path[0])
        result.append(f'(polyline (pts {" ".join("(xy " + p(v) + ")" for v in path)}) {stroke} (fill (type {fill})))')
    return result


@traced('export.kicad_symbol', lambda component, *a, **kw: {'part': safe_part(component.part), 'format': 'KICAD_SYMBOL', 'stage': 'convert'}, level='INFO')
def symbol_definition(component, name, footprint='', check=lambda: None, *, electrical_policy='source'):
    symbol = KICAD.require(component, 'symbol')
    reference = symbol.reference.rstrip('?0123456789') or 'U'
    pins = [i for unit in symbol.units for i in unit.items if isinstance(i, Pin)]
    hide_names = all(not pin.name_visible for pin in pins)
    hide_numbers = all(not pin.number_visible for pin in pins)
    result = [f'(symbol {q(name)} (pin_names (offset 0.508) {"hide" if hide_names else ""}) '
              f'{"(pin_numbers hide)" if hide_numbers else ""} (in_bom yes) (on_board yes)']
    attributes = {'Reference': reference, 'Value': component.title or symbol.name,
                  'Footprint': footprint, 'Datasheet': '', 'SupplierPart': component.part,
                  'Description': component.description, 'Source': component.source}
    attributes.update({p.name: p.value for p in symbol.properties
                       if p.name not in {'name', 'pre', 'package', *attributes}})
    if symbol.licence:
        attributes['LibraryLicence'] = symbol.licence
    all_points = []
    for unit in symbol.units:
        for item in unit.items:
            if isinstance(item, Pin):
                all_points.extend((item.connection, item.body))
            elif item.kind == 'text':
                all_points.extend(Point(v.x, v.y + item.text_style.height) for v in item.points)
            else:
                all_points.extend(v for path in contours(item, check) for v in path)
    y = max((p.y for p in all_points), default=D(0)) + D('2.54')
    for index, (key, value) in enumerate(attributes.items()):
        result.append(f'(property {q(key)} {q(value)} (id {index}) (at 0 {n(y + (D("2.54") if index == 0 else 0))} 0) '
                      f'(effects (font (size 1.27 1.27)) {"hide" if index > 1 else ""}))')
    for unit in symbol.units:
        check_extensions(unit)
        result.append(f'(symbol {q(name + "_" + str(unit.number) + "_1")}')
        for item in unit.items:
            check()
            if not isinstance(item, Pin):
                result.extend(symbol_graphic(item, check))
                continue
            check_extensions(item)
            dx, dy = item.body.x - item.connection.x, item.body.y - item.connection.y
            if dx and dy or not (dx or dy):
                raise DownloadError('KiCad 引脚仅支持非零的水平或垂直线段')
            angle = (0 if dx > 0 else 180) if dx else (90 if dy > 0 else 270)
            electrical = pin_electrical_type(item.electrical, electrical_policy)
            graphic = 'inverted_clock' if item.inverted and item.clock else 'inverted' if item.inverted else 'clock' if item.clock else 'line'
            result.append(f'(pin {electrical} {graphic} (at {p(item.connection)} {angle}) (length {n(abs(dx) + abs(dy))}) '
                          f'{"hide" if not item.visible else ""} '
                          f'(name {q(item.name)} {effects(item.name_style, hide=not item.name_visible and not hide_names, pin=True)}) '
                          f'(number {q(item.number)} {effects(item.number_style, hide=not item.number_visible and not hide_numbers, pin=True)}))')
        result.append(')')
    result.append(')')
    return '\n'.join(result)


def symbol_library(definitions):
    return (f'(kicad_symbol_lib (version {SYMBOL_VERSION}) (generator lcsc3d)\n'
            + '\n'.join(definitions) + '\n)\n').encode('utf-8')


def pad_outline(item):
    if item.shape == 'polygon':
        return list(item.vertices)
    w, h = item.size.x, item.size.y
    if item.shape == 'rect':
        vertices = [Point(-w/2, -h/2), Point(w/2, -h/2), Point(w/2, h/2), Point(-w/2, h/2)]
    elif item.shape == 'ellipse':
        vertices = ellipse(Point(), Point(w/2, h/2))
    elif item.shape == 'oval':
        # A capsule is the Minkowski sum of a line and a circle.
        radius, shift = min(w, h) / 2, abs(w - h) / 2
        circle = ellipse(Point(), Point(radius, radius))
        vertices = [Point(v.x + (shift if v.x >= 0 else -shift), v.y) if w >= h else
                    Point(v.x, v.y + (shift if v.y >= 0 else -shift)) for v in circle]
    elif item.shape == 'octagon':
        k = D(str(math.sqrt(2) - 1))
        vertices = [Point(x*w/2, y*h/2) for x, y in ((-k,-1),(k,-1),(1,-k),(1,k),(k,1),(-k,1),(-1,k),(-1,-k))]
    else:
        raise DownloadError('KiCad 焊盘形状不受支持：' + item.shape)
    return [Point(p.x + item.copper_center.x, p.y + item.copper_center.y)
            for p in (rotate(v, item.rotation) for v in vertices)]


def pad_definition(item, check):
    check_extensions(item)
    if item.layer not in ('top_copper', 'bottom_copper', 'all_copper'):
        raise DownloadError('KiCad 焊盘需要顶层、底层或多层铜层')
    if min(item.size.x, item.size.y) <= 0 or item.drill < 0:
        raise DownloadError('KiCad 焊盘或钻孔尺寸无效')
    if item.drill and item.layer != 'all_copper':
        raise DownloadError('KiCad 首版暂不支持单面铜层的钻孔焊盘')
    if not item.drill and item.layer == 'all_copper':
        raise DownloadError('KiCad 首版暂不支持无孔的全铜层焊盘')
    pad_type = ('thru_hole' if item.plated else 'np_thru_hole') if item.drill else 'smd'
    layers = '"*.Cu" "*.Mask"' if item.drill else ('"F.Cu" "F.Paste" "F.Mask"' if item.layer == 'top_copper' else '"B.Cu" "B.Paste" "B.Mask"')
    rotation = item.drill_rotation if item.slot_length else item.rotation
    center = item.center
    relative_rotation = (item.rotation - rotation) % 180
    offset = item.copper_center != center
    native = item.shape in ('rect', 'oval') or item.shape == 'ellipse' and item.size.x == item.size.y
    custom = not native or offset or relative_rotation not in (0, 90)
    size = item.size
    if relative_rotation == 90:
        size = Point(size.y, size.x)
    primitives = ''
    if custom:
        vertices = pad_outline(item)
        anchor, clearance = polygon_anchor([xy(v) for v in vertices], xy(center), tolerance=TOLERANCE, check_cancelled=check)
        if item.drill and anchor != xy(center):
            raise DownloadError('KiCad 自定义焊盘的钻孔中心不在铜区域内')
        if not item.drill:
            center = Point(*(decimal(v) for v in anchor))
        diameter = min(clearance, min(xy(item.size)) / 2)
        if diameter < 0.000002:
            raise DownloadError('KiCad 自定义焊盘缺少可靠的电气锚点')
        size = Point(decimal(diameter), decimal(diameter))
        local = [rotate(Point(v.x - center.x, v.y - center.y), -rotation) for v in vertices]
        primitives = ('(options (clearance outline) (anchor circle)) (primitives '
                      f'(gr_poly (pts {" ".join("(xy " + p(v, pcb=True) + ")" for v in local)}) (width 0) (fill yes)))')
    drill = ''
    if item.drill:
        if item.slot_length:
            if item.slot_length < item.drill:
                raise DownloadError('KiCad 槽孔长度小于孔径')
            drill = f'(drill oval {n(item.slot_length)} {n(item.drill)})'
        else:
            drill = f'(drill {n(item.drill)})'
    shape = 'custom' if custom else 'circle' if item.shape == 'ellipse' else item.shape
    return (f'(pad {q(item.number)} {pad_type} {shape} (at {p(center, pcb=True)} {n(rotation)}) '
            f'(size {p(size)}) {drill} (layers {layers}) '
            f'(solder_mask_margin {n(item.solder_margin)}) (solder_paste_margin {n(item.paste_margin)}) {primitives})')


@traced('export.kicad_footprint', lambda component, *a, **kw: {'part': safe_part(component.part), 'format': 'KICAD_FOOTPRINT', 'stage': 'convert'}, level='INFO')
def footprint_definition(component, name, check=lambda: None, *, model_assets=None, model_mode='embedded'):
    if model_assets is None and any(m.transform for m in component.models):
        raise DownloadError('已解析的模型缺少资源，无法写入 KiCad 封装')
    footprint = KICAD.require(component, 'footprint')
    from lcsc3d.models.model_export import resolved_models
    models = list(resolved_models(component, model_assets)) if model_assets is not None else []
    version = 20241229 if models and model_mode == 'embedded' else FOOTPRINT_VERSION
    description = '\n'.join(filter(None, (component.description, 'Source: ' + component.source,
                                         'LibraryLicence: ' + footprint.licence if footprint.licence else '')))
    result = [f'(footprint {q(name)} (version {version}) (generator lcsc3d) (layer "F.Cu")',
              f'(descr {q(description)}) (tags {q(component.part)})',
              '(fp_text reference "REF**" (at 0 0) (layer "F.SilkS") hide (effects (font (size 1 1) (thickness 0.15))))',
              f'(fp_text value {q(name)} (at 0 0) (layer "F.Fab") hide (effects (font (size 1 1) (thickness 0.15))))']
    for item in footprint.items:
        check()
        check_extensions(item)
        if isinstance(item, Via):
            # Footprint files cannot contain board vias. Preserve plated copper
            # and drill geometry as an unnumbered through-hole, as KiCad's own
            # Altium footprint importer does; never invent a schematic pin.
            result.append(f'(pad "" thru_hole circle (at {p(item.center, pcb=True)}) '
                          f'(size {n(item.diameter)} {n(item.diameter)}) (drill {n(item.drill)}) '
                          '(layers "*.Cu" "*.Mask"))')
            continue
        if isinstance(item, Pad):
            result.append(pad_definition(item, check))
            continue
        if not item.visible:
            continue
        is_cutout = item.kind == 'region' and item.region_type == 'npth'
        if item.layer not in LAYERS and not is_cutout:
            raise DownloadError('KiCad 图层不受支持：' + item.layer)
        target = '"Edge.Cuts"' if is_cutout else q(LAYERS[item.layer])
        if item.kind == 'text':
            result.append(f'(fp_text user {q(item.text)} (at {p(item.points[0], pcb=True)} {n(item.rotation)}) '
                          f'(layer {target}) {effects(item.text_style, pcb=True, mirrored=item.mirrored)})')
            continue
        if item.kind == 'region' and item.region_type not in ('solid', 'cutout', 'npth'):
            raise DownloadError('KiCad 首版暂不支持区域类型：' + item.region_type)
        stroke = f'(stroke (width {n(item.stroke.width)}) (type default))'
        if item.kind == 'ellipse' and item.radius.x == item.radius.y:
            pos = item.points[0]
            filled = item.fill != 'none'
            if filled:
                stroke = '(stroke (width 0) (type default))'
            result.append(f'(fp_circle (center {p(pos, pcb=True)}) (end {p(Point(pos.x + item.radius.x, pos.y), pcb=True)}) '
                          f'{stroke} (fill {"solid" if filled else "none"}) (layer {target}))')
            continue
        for path in contours(item, check):
            if len(path) < 2:
                raise DownloadError('KiCad 封装图元缺少顶点')
            if item.kind == 'region' and item.region_type == 'cutout' and (
                    item.layer.endswith('_copper') or item.layer.startswith('inner_copper_')):
                vertices = path[:-1] if path[-1] == path[0] else path
                result.append(f'(zone (net 0) (net_name "") (layer {target}) (hatch edge 0.5) '
                              '(connect_pads (clearance 0)) (min_thickness 0.25) '
                              '(keepout (tracks allowed) (vias allowed) (pads allowed) '
                              '(copperpour not_allowed) (footprints allowed)) '
                              f'(polygon (pts {" ".join("(xy " + p(v, pcb=True) + ")" for v in vertices)})))')
                continue
            # Source component-body projection layers are documentation, not
            # board cutouts. Render outer and inner contours together on Fab.
            outline_only = item.kind == 'region' and (item.layer in BODY_LAYERS or item.region_type == 'npth')
            if item.kind == 'region' and item.region_type == 'cutout' and not outline_only:
                raise DownloadError('KiCad 暂不支持此图层的挖空区域：' + item.layer)
            if outline_only:
                if path[-1] != path[0]:
                    path.append(path[0])
                edge_layer = '"Edge.Cuts"' if item.region_type == 'npth' else target
                for first, last in zip(path, path[1:]):
                    result.append(f'(fp_line (start {p(first, pcb=True)}) (end {p(last, pcb=True)}) '
                                  f'(stroke (width 0.05) (type default)) (layer {edge_layer}))')
                continue
            if item.fill != 'none':
                if len(path) < 3:
                    raise DownloadError('KiCad 填充区域缺少顶点')
                vertices = path[:-1] if path[-1] == path[0] else path
                result.append(f'(fp_poly (pts {" ".join("(xy " + p(v, pcb=True) + ")" for v in vertices)}) '
                              f'(stroke (width 0) (type default)) (fill solid) (layer {target}))')
            if item.kind != 'region' and item.stroke.width > 0:
                for first, last in zip(path, path[1:]):
                    result.append(f'(fp_line (start {p(first, pcb=True)}) (end {p(last, pcb=True)}) {stroke} (layer {target}))')
    if models:
        from lcsc3d.models.model_geometry import euler_xyz
        if model_mode not in ('embedded', 'external'):
            raise DownloadError('KiCad 模型兼容模式无效')
        embedded = {}
        for model,asset,raw in models:
            check()
            if len(model.scale) != 3 or min(model.scale) <= 0:
                raise DownloadError('KiCad 模型缩放数据无效')
            rotation = [[float(model.transform[i*4+j])/float(model.scale[j]) for j in range(3)] for i in range(3)]
            angles = euler_xyz(rotation)
            filename = model.asset_id + '.step'
            path = ('kicad-embed://' if model_mode == 'embedded' else 'LCSC3D.3dshapes/') + filename
            result.append(f'(model {q(path)} (offset (xyz {" ".join(n(model.transform[i*4+3]) for i in range(3))})) '
                          f'(scale (xyz {" ".join(model_number(v) for v in model.scale)})) '
                          f'(rotate (xyz {" ".join(n(-v) for v in angles)})))')
            if model_mode == 'embedded' and filename not in embedded:
                import zstandard
                encoded = cached_model(('kicad-step-zstd15', model.asset_id),
                    lambda: base64.b64encode(zstandard.ZstdCompressor(level=15,write_content_size=True).compress(raw)).decode('ascii'),
                    check=check)
                embedded[filename] = f'(file (name {q(filename)}) (type model) (data |{encoded}|) (checksum {q(model.asset_id)}))'
        if embedded:
            result.append('(embedded_fonts no)')
            result.append('(embedded_files '+ '\n'.join(embedded.values()) + ')')
    result.append(')')
    return ('\n'.join(result) + '\n').encode('utf-8')


def conversion_warnings(component, formats):
    messages = []
    if 'KICAD_FOOTPRINT' in formats and component.footprint and any(isinstance(i, Via) for i in component.footprint.items):
        messages.append('封装过孔按无编号镀通孔输出')
    if 'KICAD_FOOTPRINT' in formats and component.footprint and any(
            isinstance(i, Graphic) and i.kind == 'region' and i.layer in BODY_LAYERS for i in component.footprint.items):
        messages.append('器件外形投影层按装配轮廓输出')
    items = []
    if 'KICAD_SYMBOL' in formats and component.symbol:
        items += [i for u in component.symbol.units for i in u.items]
    if 'KICAD_FOOTPRINT' in formats and component.footprint:
        items += list(component.footprint.items)
    if any(isinstance(i, Graphic) and (i.kind == 'path' or i.kind == 'ellipse' and i.radius.x != i.radius.y) for i in items):
        messages.append('曲线按 0.00127 mm 公差离散')
    return tuple(messages)


def semantic_key(value):
    """Conservative physical identity; unknown fields/extensions stay significant."""
    if isinstance(value, Extension) and value.namespace == 'org.easyeda.standard.source-record' and not value.required:
        return None  # Imported provenance includes page origins and random source IDs.
    if is_dataclass(value):
        return (type(value).__name__, tuple((f.name, semantic_key(getattr(value, f.name)))
                for f in fields(value) if f.name not in {'source_id', 'licence'} and
                not (type(value).__name__ == 'Footprint' and f.name == 'name')))
    if isinstance(value, tuple):
        return tuple(semantic_key(v) for v in value)
    return value


@dataclass(frozen=True)
class Artifact:
    path: str
    payload: bytes
    format: str
    parts: tuple[str, ...]


@reuse_models
def build_library(components, requested, name, check=lambda: None, *, electrical_policy='source',
                  symbol_libraries=None, footprint_libraries=None, footprint_references=None,
                  model_assets=None, model_mode='embedded'):
    """Plan names and links before producing any file; report errors per domain."""
    name = library_name(name)
    artifacts, errors, footprint_names, used_names, known = [], {}, dict(footprint_references or {}), set(), {}
    symbol_libraries = symbol_libraries or {c.part: name for c in components}
    footprint_libraries = footprint_libraries or {c.part: name for c in components}
    for component in components:
        if 'KICAD_FOOTPRINT' not in requested[component.part]:
            continue
        try:
            footprint = require_domain(component, 'footprint')
            library = footprint_libraries[component.part]
            from lcsc3d.models.model_export import model_key, resolved_models
            signature = (library.casefold(), semantic_key(footprint), model_key(component) if model_assets is not None else ())
            target = known.get(signature)
            if target is None:
                candidate = identifier(footprint.name, component.part + '_Footprint')
                target, index = candidate, 2
                while (library.casefold(), target.casefold()) in used_names:
                    target, index = candidate + '_' + str(index), index + 1
                payload = footprint_definition(component, target, check, model_assets=model_assets, model_mode=model_mode)
                used_names.add((library.casefold(), target.casefold()))
                known[signature] = target
                artifacts.append(Artifact(library + '.pretty/' + target + '.kicad_mod', payload, 'KICAD_FOOTPRINT', (component.part,)))
            else:
                index = next(i for i, a in enumerate(artifacts) if a.path == library + '.pretty/' + target + '.kicad_mod')
                old = artifacts[index]
                artifacts[index] = Artifact(old.path, old.payload, old.format, old.parts + (component.part,))
            footprint_names[component.part] = PurePosixPath(library).name + ':' + target
            if model_assets is not None and model_mode == 'external':
                for model,asset,raw in resolved_models(component,model_assets):
                    path = library + '.pretty/LCSC3D.3dshapes/' + model.asset_id + '.step'
                    index = next((i for i,a in enumerate(artifacts) if a.path == path),None)
                    if index is None:
                        artifacts.append(Artifact(path,raw,'KICAD_MODEL',(component.part,)))
                    elif component.part not in artifacts[index].parts:
                        old = artifacts[index]
                        artifacts[index] = Artifact(old.path,old.payload,old.format,old.parts+(component.part,))
        except DownloadError as exc:
            record_error(exc, 'export.kicad_conversion_failed', part=safe_part(component.part), format='KICAD_FOOTPRINT', stage='convert')
            errors[(component.part, 'KICAD_FOOTPRINT')] = str(exc)
    definitions, members = {}, {}
    used_names = set()
    for component in components:
        if 'KICAD_SYMBOL' not in requested[component.part]:
            continue
        try:
            symbol = require_domain(component, 'symbol')
            library = symbol_libraries[component.part]
            candidate = identifier(symbol.name, component.part)
            target = candidate
            if (library.casefold(), target.casefold()) in used_names:
                target += '_' + component.part
            if (library.casefold(), target.casefold()) in used_names:
                raise DownloadError('KiCad 符号名称冲突')
            definitions.setdefault(library, []).append(symbol_definition(component, target,
                footprint_names.get(component.part, ''), check, electrical_policy=electrical_policy))
            used_names.add((library.casefold(), target.casefold()))
            members.setdefault(library, []).append(component.part)
        except DownloadError as exc:
            record_error(exc, 'export.kicad_conversion_failed', part=safe_part(component.part), format='KICAD_SYMBOL', stage='convert')
            errors[(component.part, 'KICAD_SYMBOL')] = str(exc)
    for library, items in definitions.items():
        if items:
            artifacts.append(Artifact(library + '.kicad_sym', symbol_library(items), 'KICAD_SYMBOL', tuple(members[library])))
    return artifacts, errors
