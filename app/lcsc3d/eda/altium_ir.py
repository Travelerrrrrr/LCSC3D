"""AD target adapter. Native record framing lives in altium; input is EDA IR."""
from __future__ import annotations

import math
import struct
import uuid

from lcsc3d.eda.eda_ir import D, Point, Pin, Pad, Via, require_domain, check_extensions, graphic_paths, decimal, xy
from lcsc3d.eda.eda_geometry import polygon_anchor, path_points
from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.symbol_style import symbol_style as resolve_style
from lcsc3d.eda.eda_targets import ALTIUM
from lcsc3d.core.app_logging import log_event, safe_part

UNIT = D('0.254')  # AD DXP schematic unit is 10 mil.
LAYERS = {'top_copper': 1, 'bottom_copper': 32, 'top_silk': 33, 'bottom_silk': 34,
          'top_paste': 35, 'bottom_paste': 36, 'top_mask': 37, 'bottom_mask': 38,
          'outline': 57, 'all_copper': 74, 'documentation': 61, 'top_assembly': 63,
          'bottom_assembly': 64, 'mechanical': 58, 'component_shape': 65,
          'top_component_shape': 68, 'top_component_mark': 67,
          **{f'inner_copper_{n}': n + 1 for n in range(1, 31)}}


def layer(value):
    if value not in LAYERS:
        raise DownloadError('AD 导出暂不支持 EDA 图层 ' + value)
    return LAYERS[value]


def raw(value):
    from lcsc3d.eda import altium as a
    return a._raw(decimal(value) / UNIT)


def point(p):
    return raw(p.x), raw(p.y)


def coord(record, name, value):
    from lcsc3d.eda import altium as a
    a._coord_parameter(record, name, decimal(value) / UNIT)


def source_parameters(component, licence):
    return {'Source': component.source, 'JLCEDA': 'https://lceda.cn/', 'EasyEDA': 'https://easyeda.com/',
            **({'LibraryLicence': licence} if licence else {})}


def schematic(component, check, *, merged=False, merge_pcb=False, fonts=None, footprint_name=None, symbol_style='jlc'):
    from lcsc3d.eda import altium as a
    symbol = ALTIUM.require(component, 'symbol')
    style = resolve_style(symbol_style)
    part, title = component.part, component.title
    name = a._library_name(symbol.name, part)
    footprint = a._library_name(component.footprint_name or (component.footprint.name if component.footprint else ''),
                                part + '_Footprint')
    if merge_pcb:
        footprint = a.merged_name(footprint, part)
    if footprint_name is not None:
        footprint = footprint_name
    if fonts is None:
        fonts = [('Times New Roman', 8)]
    def font(text=None):
        pair = (style.font if style else text.family or 'Times New Roman',
                text.height * 72 / D('25.4')) if text is not None else ('Times New Roman', 8)
        if pair not in fonts:
            fonts.append(pair)
        return fonts.index(pair) + 1
    def color(value, role='outline'):
        return a._color(getattr(style, role) if style else value)
    def fill_color(value):
        return a._color(style.fill(value) if style else value)
    def width(value):
        return style.line_width if style else min(3, max(0, round(value / UNIT)))
    records = [a._parameters({'RECORD': 1, 'LibReference': name, 'ComponentDescription': component.description,
                             'PartCount': len(symbol.units) + 1, 'DisplayModeCount': 1, 'IndexInSheet': -1,
                             'OwnerPartId': -1, 'CurrentPartId': 1, 'LibraryPath': '*', 'SourceLibraryName': '*',
                             'SheetPartFileName': '*', 'TargetFileName': '*', 'UniqueID': uuid.uuid4().hex[:8].upper()})]
    rectangles = []
    owner = 1
    def common(kind):
        return {'RECORD': kind, 'OwnerIndex': 0, 'OwnerPartId': owner, 'IsNotAccesible': 'T'}
    def polyline(vertices, item, closed=False):
        if len(vertices) < 2:
            raise DownloadError('符号线段缺少顶点')
        line_style = {'solid': 0, 'dash': 1, 'dot': 2, 'dash_dot': 3}.get(item.stroke.style)
        if line_style is None:
            raise DownloadError('AD 符号线型不受支持')
        record = common(7 if closed and item.fill != 'none' else 6)
        record.update(LineWidth=width(item.stroke.width), Color=color(item.stroke.color),
                      LineStyle=line_style, LocationCount=len(vertices))
        if item.fill != 'none':
            record.update(IsSolid='T', AreaColor=fill_color(item.fill))
        for index, (px, py) in enumerate(vertices, 1):
            coord(record, f'X{index}', px)
            coord(record, f'Y{index}', py)
        records.append(a._parameters(record))
    for unit in symbol.units:
        owner = unit.number
        check_extensions(unit)
        for item in unit.items:
            check()
            check_extensions(item)
            if isinstance(item, Pin):
                dx, dy = item.connection.x - item.body.x, item.connection.y - item.body.y
                if dx and dy:
                    raise DownloadError('AD 引脚仅支持 0/90/180/270 度方向')
                length = decimal(math.hypot(float(dx / UNIT), float(dy / UNIT))) * UNIT
                if length <= 0:
                    raise DownloadError('符号引脚长度必须大于零')
                if style:
                    length = max(length, decimal(style.min_pin_length) * UNIT)
                orientation = (0 if dx > 0 else 2) if dx else (1 if dy > 0 else 3)
                flags = orientation | 0x20 | (8 if item.name_visible else 0) | (16 if item.number_visible else 0)
                if not item.visible:
                    flags |= 4
                record = common(2)
                record.update(Name=item.name, Designator=item.number, Electrical=4, FormalType=1,
                              PinConglomerate=flags, Color=color(item.color, 'pin'),
                              Symbol_InnerEdge=3 if item.clock else 0, Symbol_OuterEdge=1 if item.inverted else 0,
                              PinName_PositionConglomerate=16, Name_CustomFontID=font(item.name_style),
                              Name_CustomColor=color(item.name_style.color, 'text'), PinDesignator_PositionConglomerate=16,
                              Designator_CustomFontID=font(item.number_style),
                              Designator_CustomColor=color(item.number_style.color, 'text'))
                coord(record, 'Location.X', item.body.x)
                coord(record, 'Location.Y', item.body.y)
                coord(record, 'PinLength', length)
                records.append(a._parameters(record))
                continue
            if not item.visible:
                continue
            if item.kind == 'rect':
                if min(item.radius.x, item.radius.y) < 0:
                    raise DownloadError('符号矩形圆角半径无效')
                rounded = item.radius.x > 0 and item.radius.y > 0 and not (style and style.square_rectangles)
                record = common(10 if rounded else 14)
                p = item.points[0]
                for key, value in (('Location.X', p.x), ('Location.Y', p.y),
                                   ('Corner.X', p.x + item.size.x), ('Corner.Y', p.y + item.size.y)):
                    coord(record, key, value)
                if rounded:
                    coord(record, 'CornerXRadius', item.radius.x)
                    coord(record, 'CornerYRadius', item.radius.y)
                if style:
                    record.update(Color=color(item.stroke.color), LineWidth=style.rectangle_line_width,
                                  LineStyle=0, AreaColor=a._color(style.area),
                                  IsSolid='T' if style.rectangle_filled else 'F',
                                  Transparent='T' if style.rectangle_transparent else 'F')
                else:
                    record.update(Color=color(item.stroke.color), LineWidth=width(item.stroke.width),
                                  LineStyle={'solid': 0, 'dash': 1, 'dot': 2, 'dash_dot': 3}[item.stroke.style],
                                  Transparent='T' if item.fill == 'none' else 'F')
                    if item.fill != 'none':
                        record.update(IsSolid='T', AreaColor=fill_color(item.fill))
                (rectangles if style else records).append(a._parameters(record))
            elif item.kind in ('polyline', 'polygon'):
                vertices = [(p.x, p.y) for p in item.points]
                if item.kind == 'polygon' and vertices and vertices[-1] != vertices[0]:
                    vertices.append(vertices[0])
                polyline(vertices, item, item.kind == 'polygon')
            elif item.kind == 'text':
                rotation = item.rotation % 360
                if rotation % 90:
                    raise DownloadError('AD 符号文字暂不支持非直角旋转')
                record = common(4)
                record.update(Text=item.text, FontID=font(item.text_style), Color=color(item.text_style.color, 'text'),
                              Orientation=int(rotation / 90), Justification={'start': 0, 'middle': 1, 'end': 2}.get(item.justify, 0))
                coord(record, 'Location.X', item.points[0].x)
                coord(record, 'Location.Y', item.points[0].y)
                records.append(a._parameters(record))
            elif item.kind == 'ellipse':
                record = common(8)
                coord(record, 'Location.X', item.points[0].x)
                coord(record, 'Location.Y', item.points[0].y)
                coord(record, 'Radius', item.radius.x)
                coord(record, 'SecondaryRadius', item.radius.y)
                record.update(Color=color(item.stroke.color), LineWidth=width(item.stroke.width))
                if item.fill != 'none':
                    record.update(AreaColor=fill_color(item.fill), IsSolid='T')
                records.append(a._parameters(record))
            elif item.kind == 'path':
                for vertices in graphic_paths(item, check):
                    polyline(vertices, item, bool(vertices and vertices[0] == vertices[-1]))
            else:
                raise DownloadError('AD 符号导出暂不支持图元 ' + item.kind)
    records[1:1] = rectangles
    params = {p.name: p.value for p in symbol.properties}
    parameters = {'Comment': title or name, 'SupplierPart': part,
                  'Manufacturer': params.get('Manufacturer', ''), 'ManufacturerPart': params.get('Manufacturer Part', ''),
                  'Footprint': footprint, **source_parameters(component, symbol.licence)}
    if component.description:
        parameters['Description'] = component.description
    parameters.update({key: value for key, value in params.items() if key not in ('name', 'pre', 'package')})
    designator = common(34)
    designator.update(OwnerPartId=-1, Name='Designator', Text=symbol.reference, FontID=1,
                      **{'Location.X': 0, 'Location.Y': 0, 'IsHidden': 'F'})
    if style:
        designator.update(Color=a._color(style.text), FontID=font())
    records.append(a._parameters(designator))
    log_event('DEBUG', 'export.schlib_designator', part=safe_part(part), visible=True)
    for key, value in parameters.items():
        record = common(41)
        record.update(OwnerPartId=-1, Name=key, Text=value, FontID=1,
                      **{'Location.X': 0, 'Location.Y': 0, 'IsHidden': 'T'})
        if style:
            record.update(Color=a._color(style.text), FontID=font())
        records.append(a._parameters(record))
    if component.has_footprint or component.footprint is not None or component.footprint_name:
        list_index = len(records)
        records.append(a._parameters({'RECORD': 44, 'OwnerIndex': 0}))
        implementation_index = len(records)
        records.append(a._parameters({'RECORD': 45, 'OwnerIndex': list_index, 'ModelName': footprint,
                                     'ModelType': 'PCBLIB', 'IsCurrent': 'T', 'DataFileCount': 0}))
        records.append(a._parameters({'RECORD': 46, 'OwnerIndex': implementation_index}))
        records.append(a._parameters({'RECORD': 48, 'OwnerIndex': implementation_index}))
    header = {'HEADER': 'Protel for Windows - Schematic Library Editor Binary File Version 5.0',
              'Weight': len(records), 'MinorVersion': 2, 'UniqueID': uuid.uuid4().hex[:8].upper(),
              'FontIDCount': len(fonts), 'UseMBCS': 'T', 'IsBOC': 'T', 'SheetStyle': 9,
              'BorderOn': 'T', 'SheetNumberSpaceSize': 12, 'Display_Unit': 0,
              'CustomX': 18000, 'CustomY': 18000, 'UseCustomSheet': 'T', 'ReferenceZonesOn': 'T',
              'AreaColor': 0xffffff, 'SystemFont': 1, 'SnapGridOn': 'T', 'SnapGridSize': 10,
              'VisibleGridOn': 'T', 'VisibleGridSize': 10,
              'CompCount': 1, 'LibRef0': name, 'CompDescr0': component.description, 'PartCount0': len(symbol.units) + 1}
    for index, (family, size) in enumerate(fonts, 1):
        header[f'FontName{index}'], header[f'Size{index}'] = family, round(size)
    section = a._section(part + '_Symbol') if merged else a._section(name)
    return header, {'FileHeader': a._parameters(header),
                    'SectionKeys': a._parameters({'KeyCount': 1, 'LibRef0': name, 'SectionKey0': section}),
                    section + '/Data': b''.join(records), 'Storage': a._parameters({'HEADER': 'Icon storage'})}


def pcb(component, check, *, merged=False, footprint_name=None, model_assets=None):
    if model_assets is None and any(m.transform for m in component.models):
        raise DownloadError('已解析的模型缺少资源，无法写入 AD 封装')
    from lcsc3d.eda import altium as a
    footprint = ALTIUM.require(component, 'footprint')
    name = a._library_name(footprint.name, component.part + '_Footprint')
    if merged:
        name = a.merged_name(name, component.part)
    if footprint_name is not None:
        name = footprint_name
    records, texts = [], []
    for item in footprint.items:
        check()
        check_extensions(item)
        if isinstance(item, Via):
            records.append(a._via_record(point(item.center), raw(item.diameter), raw(item.drill)))
            continue
        target_layer = layer(item.layer)
        if isinstance(item, Pad):
            if target_layer not in (1, 32, 74):
                raise DownloadError('AD 焊盘需要顶层、底层或多层图层')
            rotation = float(item.rotation)
            center, size = item.center, point(item.size)
            dx, dy = item.copper_center.x - center.x, item.copper_center.y - center.y
            angle = math.radians(rotation)
            offset = (raw(math.cos(angle) * float(dx) + math.sin(angle) * float(dy)),
                      raw(-math.sin(angle) * float(dx) + math.cos(angle) * float(dy)))
            drill, slot = raw(item.drill), raw(item.slot_length)
            slot_angle = float(item.drill_rotation - item.rotation) if slot else 0
            paste, solder = raw(item.paste_margin), raw(item.solder_margin)
            if item.shape == 'polygon':
                vertices = [xy(p) for p in item.vertices]
                anchor, clearance = polygon_anchor(vertices, xy(center), tolerance=0.00127, check_cancelled=check)
                if drill and anchor != xy(center):
                    raise DownloadError('自定义通孔焊盘的孔中心不在铜区域内')
                if not drill:
                    center = Point(*(decimal(v) for v in anchor))
                diameter = max(drill, 1, raw(min(clearance * 1.8, min(xy(item.size))) / 2))
                if diameter > raw(clearance * 2):
                    raise DownloadError('自定义焊盘的孔或连接锚点超出铜区域')
                index = len(records) + 1
                records.append(a._pad_record(target_layer, point(center), (diameter, diameter), item.number, 1,
                                             rotation, drill, item.plated, slot, slot_angle, paste, solder))
                for copper_layer in ((1, 2, 32) if target_layer == 74 else (target_layer,)):
                    records.append(a._region_record(copper_layer, [point(p) for p in item.vertices],
                                                    pad_index=index, paste=paste, solder=solder))
            else:
                if item.shape not in ('rect', 'ellipse', 'oval', 'octagon'):
                    raise DownloadError('AD 封装导出暂不支持焊盘形状 ' + item.shape)
                records.append(a._pad_record(target_layer, point(center), size, item.number,
                                             {'rect': 2, 'octagon': 3}.get(item.shape, 1), rotation, drill,
                                             item.plated, slot, slot_angle, paste, solder, offset))
            continue
        if not item.visible:
            continue
        if item.kind in ('polyline', 'path'):
            paths = [[xy(p) for p in item.points]] if item.kind == 'polyline' else graphic_paths(item, check)
            if any(len(p) < 2 for p in paths) or item.stroke.width <= 0:
                raise DownloadError('AD 线段尺寸无效')
            for vertices in paths:
                for first, last in zip(vertices, vertices[1:]):
                    records.append(a._track_record(target_layer, tuple(raw(v) for v in first),
                                                   tuple(raw(v) for v in last), raw(item.stroke.width)))
        elif item.kind == 'ellipse':
            if item.radius.x != item.radius.y:
                raise DownloadError('AD 封装椭圆暂不支持')
            if item.fill != 'none':
                cx, cy = xy(item.points[0])
                r = float(item.radius.x)
                contour = path_points(f'M{cx+r} {cy} A{r} {r} 0 1 0 {cx-r} {cy} A{r} {r} 0 1 0 {cx+r} {cy} Z',
                                      tolerance=0.00127, check_cancelled=check)[0][:-1]
                records.append(a._region_record(target_layer, [tuple(raw(v) for v in p) for p in contour]))
            else:
                records.append(a._arc_record(target_layer, point(item.points[0]), raw(item.radius.x), raw(item.stroke.width)))
        elif item.kind == 'region':
            paths = graphic_paths(item, check)
            if len(paths) != 1 or len(paths[0]) < 3:
                raise DownloadError('AD 区域暂不支持多轮廓路径')
            region_kind = {'solid': 0, 'cutout': 1, 'npth': 2}.get(item.region_type)
            if region_kind is None:
                raise DownloadError('AD 区域类型不受支持：' + item.region_type)
            vertices = paths[0][:-1] if paths[0][-1] == paths[0][0] else paths[0]
            records.append(a._region_record(target_layer, [tuple(raw(v) for v in p) for p in vertices], region_kind))
        elif item.kind == 'text':
            records.append(a._text_record(target_layer, point(item.points[0]), raw(item.text_style.height),
                                          raw(item.stroke.width), float(item.rotation), item.text, len(texts), item.mirrored))
            texts.append(item.text)
        elif item.kind == 'rect':
            p, size = item.points[0], item.size
            # Keep original top-left traversal for AD record compatibility.
            vertices = [Point(p.x, p.y + size.y), Point(p.x + size.x, p.y + size.y),
                        Point(p.x + size.x, p.y), p]
            if item.fill != 'none':
                records.append(a._region_record(target_layer, [point(p) for p in vertices]))
            if item.stroke.width:
                for first, last in zip(vertices, vertices[1:] + vertices[:1]):
                    records.append(a._track_record(target_layer, point(first), point(last), raw(item.stroke.width)))
        else:
            raise DownloadError('AD 封装导出暂不支持图元 ' + item.kind)
    section = a._section(name)
    version = 'PCB 6.0 Binary Library File'
    wide = {f'ENCODEDTEXT{i}': ','.join(str(unit) for unit in struct.unpack('<' + 'H' * (len(text.encode('utf-16le')) // 2),
                                                                         text.encode('utf-16le'))) for i, text in enumerate(texts)}
    streams = {
        'FileHeader': struct.pack('<I', len(version)) + a._short_string(version) + struct.pack('<dI', 5.01, 8) + a._short_string(uuid.uuid4().hex[:8].upper()),
        'SectionKeys': struct.pack('<I', 1) + a._string_block(name) + a._string_block(section),
        'Library/Header': struct.pack('<I', 1),
        'Library/Data': a._parameters(a._pcb_header()) + struct.pack('<I', 1) + a._string_block(name),
        section + '/Header': struct.pack('<I', len(records)),
        section + '/Data': a._string_block(name) + b''.join(records),
        section + '/Parameters': a._parameters({'PATTERN': name, 'HEIGHT': '0mil', 'DESCRIPTION': component.description,
                                                'ITEMGUID': '', 'REVISIONGUID': '', 'SupplierPart': component.part,
                                                **source_parameters(component, footprint.licence)}),
        section + '/WideStrings': a._parameters(wide),
    }
    for folder in ('Models', 'ModelsNoEmbed', 'Textures'):
        streams[f'Library/{folder}/Header'], streams[f'Library/{folder}/Data'] = struct.pack('<I', 0), b''
    if model_assets is not None:
        from lcsc3d.eda.altium_models import embed_models
        embed_models(component, streams, section, model_assets, check)
    return name, streams
