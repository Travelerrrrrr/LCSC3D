"""EasyEDA Standard adapter. Only this module understands '~'/'^^' records."""
from __future__ import annotations

import json
import math
import re
from dataclasses import replace

from eda_geometry import NUMBER, number, points, path_points, pad_outline
from eda_ir import (D, Point, Stroke, TextStyle, Graphic, Pin, Pad, Via, Property, check_via,
                    SymbolUnit, Symbol, Footprint, ModelPlacement, Model3D, Diagnostic, Component,
                    Extension, decimal)
from errors import DownloadError, Cancelled, EdaError
from app_logging import traced, record_error, log_event, safe_part
from model3d import document, model_reference

UNIT = D('0.254')
LAYERS = {1: 'top_copper', 2: 'bottom_copper', 3: 'top_silk', 4: 'bottom_silk',
          5: 'top_paste', 6: 'bottom_paste', 7: 'top_mask', 8: 'bottom_mask',
          10: 'outline', 11: 'all_copper', 12: 'documentation', 13: 'top_assembly',
          14: 'bottom_assembly', 15: 'mechanical', 99: 'component_shape',
          100: 'top_component_shape', 101: 'top_component_mark',
          **{n: f'inner_copper_{n - 20}' for n in range(21, 51)}}
STYLES = {0: 'solid', 1: 'dash', 2: 'dot', 3: 'dash_dot'}


def source_extension(value):
    return (Extension('org.easyeda.standard', '1', json.dumps(value, ensure_ascii=False)),)


def preserve_record(item, shape, coordinates):
    # Retain unmodelled source attributes for a future importer migration.
    # Targets never read these records; public geometry contains no login data.
    source = Extension('org.easyeda.standard.source-record', '1', json.dumps(
        {'record': shape, 'origin': [str(coordinates.x), str(coordinates.y)], 'unit_mm': str(UNIT)},
        ensure_ascii=False))
    return replace(item, extensions=item.extensions + (source,))


def length(value, default='0'):
    return decimal(value if value != '' else default) * UNIT


def layer(value):
    try:
        return LAYERS.get(int(value), 'source_layer_' + str(value))
    except ValueError as exc:
        raise DownloadError('EDA 图层编号无效') from exc


def no_fill(value):
    return 'none' if str(value).strip().casefold() == 'none' else value


def text_style(family='', size='', color='#000000', default='8'):
    return TextStyle(family, decimal(str(size or default).removesuffix('pt')) * D('25.4') / 72, color)


class Coordinates:
    def __init__(self, head):
        self.x, self.y = decimal(head.get('x', 0)), decimal(head.get('y', 0))

    def point(self, x, y):
        return Point((decimal(x) - self.x) * UNIT, (self.y - decimal(y)) * UNIT)

    def vertices(self, value):
        return tuple(self.point(x, y) for x, y in points(value))

    def path(self, value):
        if not isinstance(value, str) or len(value) > 4_000_000:
            raise DownloadError('EDA 图形路径无效或过大')
        pattern = rf'[MmLlHhVvCcSsQqTtAaZz]|{NUMBER}'
        if re.sub(pattern, '', value).strip(' ,\t\r\n'):
            raise DownloadError('EDA 路径中包含不支持的命令')
        tokens = re.findall(pattern, value)
        counts = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7}
        output, cursor, command = [], 0, ''
        while cursor < len(tokens):
            if tokens[cursor].isalpha():
                command = tokens[cursor]
                cursor += 1
            op = command.upper()
            if op == 'Z':
                output.append(command)
                command = ''
                continue
            count = counts.get(op)
            if count is None or cursor + count > len(tokens):
                raise DownloadError('EDA 路径坐标不完整')
            values = [decimal(v) for v in tokens[cursor:cursor + count]]
            cursor += count
            relative = command.islower()
            def px(v): return (v - (0 if relative else self.x)) * UNIT
            def py(v): return ((0 if relative else self.y) - v) * UNIT
            if op == 'H':
                converted = [px(values[0])]
            elif op == 'V':
                converted = [py(values[0])]
            elif op == 'A':
                converted = [values[0] * UNIT, values[1] * UNIT, -values[2],
                             values[3], 1 - values[4], px(values[5]), py(values[6])]
            else:
                converted = [px(v) if index % 2 == 0 else py(v) for index, v in enumerate(values)]
            output.append(command + ' ' + ' '.join(format(v, 'f') for v in converted))
            if op == 'M':
                command = 'l' if relative else 'L'
        return ' '.join(output)


def shapes(doc, kind):
    value = doc.get('shape')
    if not isinstance(value, list) or not value or len(value) > 100000:
        raise DownloadError(f'官方库中没有可导出的{kind}图元')
    if any(not isinstance(s, str) for s in value):
        raise DownloadError(f'{kind}图元数据格式不受支持')
    return value


def stroke(color, width, style='0'):
    style_id = int(number(style, 0))
    return Stroke(length(width, '1'), color, STYLES.get(style_id, 'source_style_' + str(style_id)))


def pin_endpoints(header, dot, endpoints):
    """SVG drawing order is not electrical direction; the pin dot is authoritative."""
    start, end = endpoints
    if start == end:
        raise DownloadError('符号引脚长度必须大于零')
    position = dot[:2] if len(dot) >= 2 and all(dot[:2]) else header[4:6]
    if len(position) != 2 or not all(position):
        raise DownloadError('符号引脚缺少连接点坐标')
    anchor = tuple(number(v) for v in position)
    distances = [math.dist(anchor, point) for point in endpoints]
    if min(distances) > 0.0001 or abs(distances[0] - distances[1]) < 1e-9:
        raise DownloadError('符号引脚连接点与线段端点不一致')
    return (start, end) if distances[0] < distances[1] else (end, start)


def symbol_item(shape, c, check):
    f = shape.split('~')
    kind = f[0]
    if kind == 'P':
        groups = [group.split('~') for group in shape.split('^^')]
        header, line, label, designator = groups[0], groups[2], groups[3], groups[4]
        paths = path_points(line[0], check_cancelled=check)
        if len(paths) != 1 or len(paths[0]) != 2:
            raise DownloadError('符号引脚必须是一条直线')
        external, body = pin_endpoints(header, groups[1], paths[0])
        return Pin(number=designator[4] if len(designator) > 4 else header[3], name=label[4],
                   connection=c.point(*external), body=c.point(*body),
                   electrical={'': 'unspecified', '0': 'unspecified', '1': 'input', '2': 'output',
                               '3': 'bidirectional', '4': 'power'}.get(header[2], 'source_type_' + header[2]),
                   visible=header[1] == 'show', name_visible=label[0] == '1', number_visible=designator[0] == '1',
                   inverted=groups[5][0] == '1', clock=groups[6][0] == '1', color=line[1],
                   name_style=text_style(label[6] or 'Verdana', label[7], label[8], '7'),
                   number_style=text_style(designator[6] or 'Verdana', designator[7], designator[8], '7'),
                   name_position=c.point(label[1], label[2]), number_position=c.point(designator[1], designator[2]),
                   source_id=header[7], extensions=source_extension({'electrical': header[2]}))
    if kind == 'R':
        return Graphic('rect', (c.point(f[1], decimal(f[2]) + decimal(f[6])),),
                       Point(length(f[5]), length(f[6])), Point(length(f[3]), length(f[4])),
                       stroke=stroke(f[7], f[8], f[9]), fill=no_fill(f[10]), source_id=f[11])
    if kind in ('PL', 'PG'):
        return Graphic('polygon' if kind == 'PG' else 'polyline', c.vertices(f[1]),
                       stroke=stroke(f[2], f[3], f[4]), fill=no_fill(f[5]), source_id=f[6])
    if kind == 'T':
        return Graphic('text', (c.point(f[2], f[3]),), text=f[12], text_style=text_style(f[6], f[7], f[5]),
                       rotation=decimal(f[4] or 0), justify=f[10], visible=f[13] == '1',
                       source_id=f[15] if len(f) > 15 else '', extensions=source_extension({'role': f[11]}))
    if kind in ('C', 'E'):
        ci = 4 if kind == 'C' else 5
        return Graphic('ellipse', (c.point(f[1], f[2]),),
                       radius=Point(length(f[3]), length(f[3] if kind == 'C' else f[4])),
                       stroke=stroke(f[ci], f[ci + 1], f[ci + 2]), fill=no_fill(f[ci + 3]),
                       source_id=f[ci + 4])
    if kind in ('A', 'PT'):
        si = 3 if kind == 'A' else 2
        return Graphic('path', path=c.path(f[1]), stroke=stroke(f[si], f[si + 1], f[si + 2]),
                       fill=no_fill(f[si + 3]), source_id=f[si + 4])
    return Graphic('unsupported', source_kind=kind, extensions=source_extension({'kind': kind, 'record': shape}))


def import_symbol(data, part, check):
    doc = document(data.get('dataStr'))
    head = document(doc.get('head'))
    params = document(head.get('c_para'))
    units = data.get('subparts') or [data]
    if not isinstance(units, list) or len(units) > 32766:
        raise DownloadError('符号单元数据格式无效或过多')
    result = []
    for index, unit in enumerate(units, 1):
        unit_doc = document(document(unit).get('dataStr'))
        c = Coordinates(document(unit_doc.get('head')))
        items = []
        for shape in shapes(unit_doc, '符号'):
            check()
            try:
                items.append(preserve_record(symbol_item(shape, c, check), shape, c))
            except (IndexError, ValueError, TypeError) as exc:
                raise EdaError('符号 ' + shape.split('~')[0] + ' 图元数据不完整或超出范围',
                    code='invalid_source_primitive', domain='symbol', primitive=shape.split('~')[0]) from exc
        result.append(SymbolUnit(index, tuple(items)))
    return Symbol(str(data.get('title') or params.get('name') or part), str(params.get('pre') or 'U?'),
                  tuple(result), tuple(Property(str(k), str(v)) for k, v in params.items()),
                  str(head.get('licence') or ''))


def import_pad(f, c, check):
    copper = (number(f[2]), number(f[3]))
    if len(f) > 19 and f[19]:
        center = points(f[19])[0]
    elif number(f[13], 0) and f[14]:
        endpoints = points(f[14])
        if len(endpoints) != 2:
            raise DownloadError('EDA 槽孔中心线无效')
        center = tuple((a + b) / 2 for a, b in zip(*endpoints))
    elif f[1] != 'POLYGON' and f[10]:
        outline = points(f[10])
        center = copper = ((min(p[0] for p in outline) + max(p[0] for p in outline)) / 2,
                           (min(p[1] for p in outline) + max(p[1] for p in outline)) / 2) if len(outline) >= 2 else copper
    else:
        center = copper
    size = Point(length(f[4]), length(f[5]))
    drill, slot = length(f[9]) * 2, length(f[13])
    if min(size.x, size.y) <= 0 or drill < 0 or slot < 0:
        raise DownloadError('EDA 焊盘或钻孔尺寸无效')
    rotation = decimal(f[11] or 0)
    native_shape = f[1]
    vertices = ()
    if native_shape == 'POLYGON':
        outline = pad_outline(points(f[10]), float(rotation), check_cancelled=check)
        vertices = tuple(c.point(*p) for p in outline.vertices)
        if outline.shape:
            native_shape, copper = outline.shape, outline.center
            size = Point(*(length(v) for v in outline.size))
            rotation = decimal(outline.rotation)
            if not drill:
                center = copper
    if f[1] not in ('RECT', 'ELLIPSE', 'OVAL', 'POLYGON'):
        native_shape = 'unsupported_' + f[1]
    drill_angle = rotation
    if slot:
        hole_points = points(f[14])
        if len(hole_points) != 2 or not drill or slot < drill:
            raise DownloadError('EDA 槽孔中心线或尺寸无效')
        a, b = hole_points
        drill_angle = decimal(math.degrees(math.atan2(a[1] - b[1], b[0] - a[0])))
    plated = f[15].strip().lower()
    if plated not in ('y', 'yes', 'true', '1', 'n', 'no', 'false', '0'):
        raise DownloadError('EDA 焊盘镀孔属性不受支持')
    return Pad(f[8], native_shape.lower(), c.point(*center), c.point(*copper), size, vertices, rotation, layer(f[6]),
               drill, slot, drill_angle, plated in ('y', 'yes', 'true', '1'),
               length(f[17]) if len(f) > 17 else D(0), length(f[18]) if len(f) > 18 else D(0),
               source_id=f[12], extensions=source_extension({'shape': f[1]}))


def footprint_item(shape, c, check):
    f = shape.split('~')
    kind = f[0]
    if kind == 'PAD':
        return import_pad(f, c, check)
    if kind == 'VIA':
        via = Via(c.point(f[1], f[2]), length(f[3]), length(f[5]) * 2, net=f[4], source_id=f[6])
        check_via(via)
        return via
    if kind == 'HOLE':
        diameter = length(f[3]) * 2
        if diameter <= 0:
            raise DownloadError('EDA 定位孔尺寸无效')
        center = c.point(f[1], f[2])
        return Pad('', 'ellipse', center, center, Point(diameter, diameter), layer='all_copper',
                   drill=diameter, plated=False, mechanical_hole=True, source_id=f[4])
    if kind == 'TRACK':
        return Graphic('polyline', c.vertices(f[4]), stroke=Stroke(length(f[1])), layer=layer(f[2]), source_id=f[5])
    if kind == 'CIRCLE':
        radius, width = length(f[3]), length(f[4])
        if radius <= 0 or width <= 0:
            raise DownloadError('EDA 圆形尺寸无效')
        return Graphic('ellipse', (c.point(f[1], f[2]),), radius=Point(radius, radius),
                       stroke=Stroke(width), fill='#000000' if f[5] == '101' and width >= 2 * radius else 'none',
                       layer=layer(f[5]), source_id=f[6])
    if kind == 'SOLIDREGION':
        return Graphic('region', path=c.path(f[3]), layer=layer(f[1]), region_type=f[4],
                       fill='#000000', source_id=f[5])
    if kind == 'ARC':
        return Graphic('path', path=c.path(f[4]), layer=layer(f[2]), stroke=Stroke(length(f[1])), source_id=f[6])
    if kind == 'TEXT':
        return Graphic('text', (c.point(f[2], f[3]),), text=f[10], text_style=TextStyle(height=length(f[9])),
                       stroke=Stroke(length(f[4])), rotation=decimal(f[5] or 0),
                       mirrored=f[6] in ('1', 'true'), layer=layer(f[7]), visible=f[13] == '1',
                       source_id=f[12])
    if kind == 'RECT':
        width = length(f[8]) if len(f) > 8 else D(0)
        filled = len(f) <= 9 or no_fill(f[9]) != 'none'
        size = Point(length(f[3]), length(f[4]))
        if min(size.x, size.y) <= 0 or width < 0 or not filled and width == 0:
            raise DownloadError('EDA 矩形区域尺寸无效')
        return Graphic('rect', (c.point(f[1], decimal(f[2]) + decimal(f[4])),), size,
                       stroke=Stroke(width), fill='#000000' if filled else 'none', layer=layer(f[5]), source_id=f[6])
    if kind == 'SVGNODE':
        # Model-node failures belong to model3d diagnostics and must not erase
        # otherwise usable pads. import_models checks these records separately.
        try:
            node = document(shape.partition('~')[2])
            attrs = document(node.get('attrs'))
        except DownloadError:
            return None
        if attrs.get('c_etype') in (None, 'outline3D'):
            return None
    return Graphic('unsupported', source_kind=kind, extensions=source_extension({'kind': kind, 'record': shape}))


def import_footprint(data, part, check):
    package = document(data.get('packageDetail'))
    doc = document(package.get('dataStr'))
    head = document(doc.get('head'))
    params = document(head.get('c_para'))
    c, items = Coordinates(head), []
    for shape in shapes(doc, '封装'):
        check()
        try:
            item = footprint_item(shape, c, check)
            if item is not None:
                items.append(preserve_record(item, shape, c))
        except (IndexError, ValueError, TypeError) as exc:
            raise EdaError('封装 ' + shape.split('~')[0] + ' 图元数据不完整或超出范围',
                code='invalid_source_primitive', domain='footprint', primitive=shape.split('~')[0]) from exc
    return Footprint(str(package.get('title') or params.get('package') or part + '_Footprint'),
                     tuple(items), str(head.get('licence') or ''))


def import_models(data, package, head):
    """Parse each explicit Standard model node, retaining failures per node."""
    models, diagnostics = [], []
    def issue(code, message, source_id=''):
        diagnostics.append(Diagnostic('model3d', 'error', code, message, source_id=source_id))
    doc = document(package.get('dataStr'))
    records = doc.get('shape') or []
    if not isinstance(records, list):
        issue('invalid_model_nodes', '3D 模型关联列表无效')
        return (), tuple(diagnostics)
    for shape in records:
        if not isinstance(shape, str) or not shape.startswith('SVGNODE~'):
            continue
        identity = ''
        if len(models) >= 128:
            issue('too_many_models','封装关联的 3D 模型数量超出支持范围')
            break
        try:
            node = document(shape.partition('~')[2])
            attrs = document(node.get('attrs'))
            if attrs.get('c_etype') not in (None, 'outline3D'):
                continue
            identity = str(attrs.get('id') or '')
            identifier = attrs.get('uuid')
            if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
                raise DownloadError('3D 模型资源编号缺失或无效')
            model = Model3D(identifier, str(attrs.get('title') or 'model'), source_id=identity,
                            extensions=source_extension({'placements': [attrs]}))
            models.append(model)
            def vector(value, count):
                if not isinstance(value, str) or len(value.split(',')) != count:
                    raise DownloadError('3D 模型放置参数缺失或格式无效')
                return tuple(decimal(v.strip()) for v in value.split(','))
            x,y = vector(attrs.get('c_origin'),2)
            angles = vector(attrs.get('c_rotation'),3)
            if any(k not in attrs or str(attrs[k]).strip() == '' for k in ('z','c_width','c_height')):
                raise DownloadError('3D 模型高度或尺寸参数缺失')
            if 'x' not in head or 'y' not in head:
                raise DownloadError('封装原点缺失，无法确定 3D 放置')
            size = (length(attrs['c_width']),length(attrs['c_height']))
            if min(size) <= 0:
                raise DownloadError('3D 模型声明尺寸必须大于零')
            placement = ModelPlacement(((x-decimal(head['x']))*UNIT,(decimal(head['y'])-y)*UNIT,
                                        length(attrs['z'])),angles,size)
            models[-1] = replace(model,placement=placement)
        except (DownloadError, ValueError, TypeError, KeyError) as exc:
            issue('invalid_model_placement', str(exc), identity)
    if not models and not diagnostics:
        try:
            reference = model_reference(data)
            if reference is not None:
                models.append(Model3D(reference.uuid,reference.name))
                issue('missing_model_placement', '官方关联了模型，但缺少可解释的放置参数')
        except DownloadError as exc:
            issue('invalid_model_reference', str(exc))
    return tuple(models),tuple(diagnostics)


@traced('eda.import', lambda data, part, *a, **kw: {'part': safe_part(part), 'domains': kw.get('domains', ('symbol', 'footprint'))})
def parse_component(data, part, check_cancelled=lambda: None, *, domains=('symbol', 'footprint')):
    if isinstance(data, Component):
        return data
    try:
        package = document(data.get('packageDetail'))
    except DownloadError:
        package = {}
    try:
        head = document(document(package.get('dataStr')).get('head'))
    except DownloadError:
        head = {}
    params = document(head.get('c_para'))
    component = Component(part, str(data.get('title') or ''), str(data.get('description') or ''),
                          source_url=str(data.get('_source_url') or ''),
                          footprint_name=str(package.get('title') or params.get('package') or part + '_Footprint') if package else '',
                          has_footprint=bool(package))
    diagnostics = []
    for domain, importer in (('symbol', import_symbol), ('footprint', import_footprint)):
        if domain not in domains:
            continue
        try:
            component = replace(component, **{domain: importer(data, part, check_cancelled)})
        except Cancelled:
            raise
        except DownloadError as exc:
            record_error(exc, 'eda.import_domain_failed', part=safe_part(part), domain=domain, stage='import')
            diagnostics.append(Diagnostic(domain, 'error', getattr(exc, 'eda_code', 'invalid_source'), str(exc),
                primitive=getattr(exc, 'eda_primitive', ''), source_id=getattr(exc, 'eda_source_id', '')))
    try:
        models, model_diagnostics = import_models(data, package, head)
        component = replace(component, models=models)
        diagnostics.extend(model_diagnostics)
    except (DownloadError, TypeError, ValueError) as exc:
        diagnostics.append(Diagnostic('model3d', 'error', 'invalid_model_reference', str(exc)))
    log_event('DEBUG', 'eda.import_result', part=safe_part(part), domains=domains,
              symbol_units=len(component.symbol.units) if component.symbol else 0,
              footprint_items=len(component.footprint.items) if component.footprint else 0,
              vias=sum(isinstance(i, Via) for i in component.footprint.items) if component.footprint else 0,
              failure_count=len(diagnostics), schema_version=component.schema_version)
    return replace(component, diagnostics=tuple(diagnostics))
