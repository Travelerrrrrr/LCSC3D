"""Export official EasyEDA geometry as native SchLib/PcbLib files.

CFB storage is supplied by Windows. Record framing and coordinate conventions
were checked against AltiumSharp; see licenses/THIRD-PARTY.md for provenance.
Unsupported input raises DownloadError before any output is committed.
"""
from __future__ import annotations

from app_logging import traced, safe_part

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
import struct
import uuid
import zlib

from compound_storage import compound_file
from eda_geometry import number, points, path_points, polygon_anchor, pad_outline
from errors import DownloadError
from model3d import document

SOURCE = 'JLCEDA/EasyEDA Official Library'
HOMEPAGES = 'https://lceda.cn/ ; https://easyeda.com/'
LAYERS = {1: 1, 2: 32, 3: 33, 4: 34, 5: 35, 6: 36, 7: 37, 8: 38,
          10: 57, 11: 74, 12: 61, 13: 63, 14: 64, 15: 58,
          99: 65, 100: 68, 101: 67, **{n: n - 19 for n in range(21, 51)}}
LAYER_NAMES = {1: 'TOP', 32: 'BOTTOM', 33: 'TOPOVERLAY', 34: 'BOTTOMOVERLAY',
               35: 'TOPPASTE', 36: 'BOTTOMPASTE', 37: 'TOPSOLDER', 38: 'BOTTOMSOLDER',
               56: 'KEEPOUT', 74: 'MULTILAYER', **{n: f'MECHANICAL{n - 56}' for n in range(57, 73)}}


def _decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise DownloadError('EDA 坐标或尺寸无效') from exc
    if not result.is_finite() or abs(result) > 10000000:
        raise DownloadError('EDA 坐标或尺寸超出支持范围')
    return result


def _raw(value):
    # EasyEDA PCB and schematic coordinates: 1 unit = 10 mil = 100000 AD raw units.
    result = int((_decimal(value) * 100000).to_integral_value(rounding=ROUND_HALF_UP))
    if not -(2**31) <= result < 2**31:
        raise DownloadError('AD 坐标或尺寸超出范围')
    return result


def _block(data, flag=0):
    if len(data) > 0xffffff:
        raise DownloadError('AD 图元记录过大')
    return struct.pack('<I', len(data) | flag << 24) + data


def _parameters(values):
    fields = []
    for key, value in values.items():
        value = str(value)
        if any(c in value for c in '|\x00'):
            raise DownloadError(f'AD 参数 {key} 中包含不支持的分隔符')
        try:
            encoded = value.encode('cp1252')
        except UnicodeEncodeError:
            key = '%UTF8%' + key
            encoded = value.encode('utf-8')
        fields.append(b'|' + key.encode('ascii') + b'=' + encoded)
    return _block(b''.join(fields) + b'\0')


def _short_string(value, replace=False):
    try:
        encoded = str(value).encode('cp1252', errors='replace' if replace else 'strict')
    except UnicodeEncodeError as exc:
        raise DownloadError('AD 短字符串含有不支持的字符') from exc
    if len(encoded) > 255:
        if replace:
            encoded = encoded[:255]  # Full PCB text remains in WideStrings.
        else:
            raise DownloadError('AD 库名称或引脚文本超过 255 字节')
    return bytes([len(encoded)]) + encoded


def _string_block(value):
    return _block(_short_string(value))


def _library_name(value, fallback):
    value = str(value or fallback).strip()
    try:
        _short_string(value)
        if any(c in value for c in '|\x00') or value.casefold() in ('library', 'fileheader', 'sectionkeys', 'storage'):
            raise DownloadError('invalid name')
        return value
    except DownloadError:
        # Native short-string indices have no Unicode variant. Preserve the human
        # name in Unicode parameters and use the stable C-number for discovery.
        return fallback


def _section(name):
    return re.sub(r'[/\\:!]', '_', name)[:31]


def _shapes(doc, kind):
    shapes = doc.get('shape')
    if not isinstance(shapes, list) or not shapes or len(shapes) > 100000:
        raise DownloadError(f'官方库中没有可导出的{kind}图元')
    for shape in shapes:
        if not isinstance(shape, str):
            raise DownloadError(f'{kind}图元数据格式不受支持')
    return shapes


def _color(value):
    value = value or '#000000'
    if re.fullmatch(r'#[0-9a-fA-F]{3}', value):
        value = '#' + ''.join(c * 2 for c in value[1:])
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise DownloadError('EDA 图元颜色不受支持：' + value)
    rgb = bytes.fromhex(value[1:])
    return rgb[0] | rgb[1] << 8 | rgb[2] << 16


def _unfilled(value):
    """Official symbols use both 'none' and 'NONE' for an absent fill."""
    return isinstance(value, str) and value.strip().casefold() == 'none'


def _coord_parameter(record, key, value):
    raw = _raw(value)
    whole = int(Decimal(raw) / 100000)
    record[key] = whole
    remainder = raw - whole * 100000
    if remainder:
        record[key + '_Frac'] = remainder


def _layer(value):
    try:
        return LAYERS[int(value)]
    except (ValueError, KeyError) as exc:
        raise DownloadError(f'AD 导出暂不支持 EDA 图层 {value}') from exc


def _v7_layer(layer):
    if layer == 32:
        return 0x0100ffff
    if 1 <= layer <= 31:
        return 0x01000000 + layer
    if 57 <= layer <= 72:
        return 0x01020000 + layer - 56
    return {33: 0x01030006, 34: 0x01030007, 35: 0x01030008, 36: 0x01030009,
            37: 0x0103000a, 38: 0x0103000b, 56: 0x0103000d, 74: 0x0103000f}[layer]


def _common(layer, keepout=False, polygon=0xffff):
    return struct.pack('<BHHHHI', layer, 0x0c | (0x0200 if keepout else 0), 0xffff, polygon, 0xffff, 0xffffffff)


def _pcb_documents(data, part):
    package = document(data.get('packageDetail'))
    doc = document(package.get('dataStr'))
    head = document(doc.get('head'))
    params = document(head.get('c_para'))
    name = _library_name(package.get('title') or params.get('package'), part + '_Footprint')
    return doc, head, name


def _source_parameters(head):
    return {'Source': SOURCE, 'JLCEDA': 'https://lceda.cn/', 'EasyEDA': 'https://easyeda.com/',
            **({'LibraryLicence': head['licence']} if head.get('licence') else {})}


@traced('export.schlib', lambda data, part, *a, **kw: {'part': safe_part(part), 'format': 'SCHLIB'}, level='INFO')
def export_schlib(data, part, check_cancelled=lambda: None):
    header, streams = _schlib_component(data, part, check_cancelled)
    streams['FileHeader'] = _parameters(header)
    return compound_file(streams, check_cancelled)


def _schlib_component(data, part, check_cancelled, *, merged=False, merge_pcb=False, fonts=None, footprint_name=None):
    doc = document(data.get('dataStr'))
    head = document(doc.get('head'))
    params = document(head.get('c_para'))
    units = data.get('subparts') or [data]
    if not isinstance(units, list) or len(units) > 32766:
        raise DownloadError('符号单元数据格式无效或过多')
    name = _library_name(data.get('title') or params.get('name'), part)
    _, _, footprint = _pcb_documents(data, part)
    if merge_pcb:
        footprint = merged_name(footprint, part)
    if footprint_name is not None:
        footprint = footprint_name
    ox, oy = _decimal(head.get('x', 0)), _decimal(head.get('y', 0))
    def x(value): return _decimal(value) - ox
    def y(value): return oy - _decimal(value)
    if fonts is None:
        fonts = [('Times New Roman', 8)]
    def font(family='', size=''):
        size = number(str(size).removesuffix('pt'), 8)
        pair = (family or 'Times New Roman', size)
        if pair not in fonts:
            fonts.append(pair)
        return fonts.index(pair) + 1

    records = [_parameters({'RECORD': 1, 'LibReference': name, 'ComponentDescription': data.get('description') or '',
                            'PartCount': len(units) + 1, 'DisplayModeCount': 1, 'IndexInSheet': -1, 'OwnerPartId': -1,
                            'CurrentPartId': 1, 'LibraryPath': '*', 'SourceLibraryName': '*',
                            'SheetPartFileName': '*', 'TargetFileName': '*', 'UniqueID': uuid.uuid4().hex[:8].upper()})]
    pin_count = 0
    owner_part = 1

    def common(kind):
        return {'RECORD': kind, 'OwnerIndex': 0, 'OwnerPartId': owner_part, 'IsNotAccesible': 'T'}

    def polyline(vertices, color, width, style='0', fill='none', closed=False):
        if len(vertices) < 2:
            raise DownloadError('符号线段缺少顶点')
        record = common(7 if closed and not _unfilled(fill) else 6)
        record.update({'LineWidth': min(3, max(0, round(number(width, 1)))), 'Color': _color(color),
                       'LineStyle': int(number(style, 0)), 'LocationCount': len(vertices)})
        if not _unfilled(fill):
            record.update({'IsSolid': 'T', 'AreaColor': _color(fill)})
        for i, (px, py) in enumerate(vertices, 1):
            # SchLib parameter vertices use the same 10-mil DXP units as
            # pin locations. Dividing raw coordinates by 1000 magnifies the
            # symbol body 100x while leaving its binary pins at normal size.
            _coord_parameter(record, f'X{i}', x(px))
            _coord_parameter(record, f'Y{i}', y(py))
        records.append(_parameters(record))

    # Each subpart has its own placement origin. The top-level shape is an
    # overview and must not be mixed into the numbered library units.
    def unit_shapes():
        for unit_index, unit in enumerate(units, 1):
            check_cancelled()
            unit_doc = document(document(unit).get('dataStr'))
            unit_head = document(unit_doc.get('head'))
            for shape in _shapes(unit_doc, '符号'):
                yield unit_index, unit_head, shape
    for owner_part, unit_head, shape in unit_shapes():
        check_cancelled()
        ox, oy = _decimal(unit_head.get('x', 0)), _decimal(unit_head.get('y', 0))
        fields = shape.split('~')
        kind = fields[0]
        try:
            if kind == 'P':
                groups = [group.split('~') for group in shape.split('^^')]
                header, line, label, designator = groups[0], groups[2], groups[3], groups[4]
                path = path_points(line[0], check_cancelled=check_cancelled)
                if len(path) != 1 or len(path[0]) != 2:
                    raise DownloadError('符号引脚必须是一条直线')
                external, body = path[0]
                bx, by = x(body[0]), y(body[1])
                dx, dy = external[0] - body[0], body[1] - external[1]
                if dx and dy:
                    raise DownloadError('AD 引脚仅支持 0/90/180/270 度方向')
                length = _decimal(math.hypot(dx, dy))
                if length <= 0:
                    raise DownloadError('符号引脚长度必须大于零')
                orientation = (0 if dx > 0 else 2) if dx else (1 if dy > 0 else 3)
                electrical = {0: 4, 1: 0, 2: 1, 3: 2, 4: 7}.get(int(number(header[2], 0)))
                if electrical is None:
                    raise DownloadError('符号引脚电气类型不受支持')
                flags = orientation | 0x20 | (8 if label[0] == '1' else 0) | (16 if designator[0] == '1' else 0)
                if header[1] != 'show':
                    flags |= 4
                pin_name, pin_number = label[4], designator[4] if len(designator) > 4 else header[3]
                # Native parameter pins retain editable pin text styles as well
                # as Unicode and fractional geometry. The compact binary form
                # cannot carry these styles without additional side streams.
                # Empty EasyEDA pin font fields mean Verdana 7pt, as in its SVG.
                record = common(2)
                record.update({'Name': pin_name, 'Designator': pin_number, 'Electrical': electrical,
                               'FormalType': 1, 'PinConglomerate': flags, 'Color': _color(line[1]),
                               'Symbol_InnerEdge': 3 if groups[6][0] == '1' else 0,
                               'Symbol_OuterEdge': 1 if groups[5][0] == '1' else 0,
                               'PinName_PositionConglomerate': 16,
                               'Name_CustomFontID': font(label[6] or 'Verdana', label[7] or '7pt'),
                               'Name_CustomColor': _color(label[8]),
                               'PinDesignator_PositionConglomerate': 16,
                               'Designator_CustomFontID': font(designator[6] or 'Verdana', designator[7] or '7pt'),
                               'Designator_CustomColor': _color(designator[8])})
                _coord_parameter(record, 'Location.X', bx)
                _coord_parameter(record, 'Location.Y', by)
                _coord_parameter(record, 'PinLength', length)
                records.append(_parameters(record))
                pin_count += 1
            elif kind == 'R':
                rx, ry = _decimal(fields[3] or '0'), _decimal(fields[4] or '0')
                if rx < 0 or ry < 0:
                    raise DownloadError('符号矩形圆角半径无效')
                rounded = rx > 0 and ry > 0
                record = common(10 if rounded else 14)
                _coord_parameter(record, 'Location.X', x(fields[1]))
                _coord_parameter(record, 'Location.Y', y(_decimal(fields[2]) + _decimal(fields[6])))
                _coord_parameter(record, 'Corner.X', x(_decimal(fields[1]) + _decimal(fields[5])))
                _coord_parameter(record, 'Corner.Y', y(fields[2]))
                # Preserve rounded corners where supplied by the official shape.
                if rounded:
                    _coord_parameter(record, 'CornerXRadius', rx)
                    _coord_parameter(record, 'CornerYRadius', ry)
                record.update({'Color': _color(fields[7]), 'LineWidth': min(3, max(0, round(number(fields[8], 1)))),
                               'LineStyle': int(number(fields[9], 0)), 'Transparent': 'T' if _unfilled(fields[10]) else 'F'})
                if not _unfilled(fields[10]):
                    record.update({'IsSolid': 'T', 'AreaColor': _color(fields[10])})
                records.append(_parameters(record))
            elif kind in ('PL', 'PG'):
                vertices = points(fields[1])
                if kind == 'PG' and vertices and vertices[-1] != vertices[0]:
                    vertices.append(vertices[0])
                polyline(vertices, fields[2], fields[3], fields[4], fields[5], kind == 'PG')
            elif kind == 'T':
                if fields[13] != '1':
                    continue
                record = common(4)
                rotation = number(fields[4], 0) % 360
                if rotation % 90:
                    raise DownloadError('AD 符号文字暂不支持非直角旋转')
                record.update({'Text': fields[12], 'FontID': font(fields[6], fields[7]),
                               'Color': _color(fields[5]), 'Orientation': int(rotation / 90),
                               'Justification': {'start': 0, 'middle': 1, 'end': 2}.get(fields[10], 0)})
                _coord_parameter(record, 'Location.X', x(fields[2]))
                _coord_parameter(record, 'Location.Y', y(fields[3]))
                records.append(_parameters(record))
            elif kind in ('C', 'E'):
                record = common(8)
                is_circle = kind == 'C'
                color_index = 4 if is_circle else 5
                _coord_parameter(record, 'Location.X', x(fields[1]))
                _coord_parameter(record, 'Location.Y', y(fields[2]))
                _coord_parameter(record, 'Radius', fields[3])
                _coord_parameter(record, 'SecondaryRadius', fields[3] if is_circle else fields[4])
                record.update({'Color': _color(fields[color_index]), 'LineWidth': min(3, max(0, round(number(fields[color_index + 1], 1))))})
                if not _unfilled(fields[color_index + 3]):
                    record.update({'AreaColor': _color(fields[color_index + 3]), 'IsSolid': 'T'})
                records.append(_parameters(record))
            elif kind in ('A', 'PT'):
                paths = path_points(fields[1], check_cancelled=check_cancelled)
                style_index = 3 if kind == 'A' else 2  # A carries an extra helper-dot field.
                for vertices in paths:
                    polyline(vertices, fields[style_index], fields[style_index + 1], fields[style_index + 2],
                             fields[style_index + 3], bool(vertices and vertices[0] == vertices[-1]))
            else:
                raise DownloadError(f'AD 符号导出暂不支持图元 {kind}')
        except (IndexError, ValueError, TypeError, struct.error) as exc:
            raise DownloadError(f'符号 {kind} 图元数据不完整或超出范围') from exc
    if not pin_count:
        raise DownloadError('官方符号没有可导出的引脚')

    parameters = {'Comment': data.get('title') or name, 'SupplierPart': part,
                  'Manufacturer': params.get('Manufacturer', ''), 'ManufacturerPart': params.get('Manufacturer Part', ''),
                  'Footprint': footprint, **_source_parameters(head)}
    if data.get('description'):
        parameters['Description'] = data['description']
    # Preserve the original vendor component attributes, including its licence.
    parameters.update({key: value for key, value in params.items() if key not in ('name', 'pre', 'package')})
    designator = common(34)
    designator.update({'OwnerPartId': -1, 'Name': 'Designator', 'Text': params.get('pre') or 'U?', 'FontID': 1,
                       'Location.X': 0, 'Location.Y': 0, 'IsHidden': 'T'})
    records.append(_parameters(designator))
    for key, value in parameters.items():
        record = common(41)
        record.update({'OwnerPartId': -1, 'Name': key, 'Text': value, 'FontID': 1,
                       'Location.X': 0, 'Location.Y': 0, 'IsHidden': 'T'})
        records.append(_parameters(record))
    if data.get('packageDetail'):
        list_index = len(records)
        records.append(_parameters({'RECORD': 44, 'OwnerIndex': 0}))
        implementation_index = len(records)
        records.append(_parameters({'RECORD': 45, 'OwnerIndex': list_index, 'ModelName': footprint,
                                    'ModelType': 'PCBLIB', 'IsCurrent': 'T', 'DataFileCount': 0}))
        records.append(_parameters({'RECORD': 46, 'OwnerIndex': implementation_index}))
        records.append(_parameters({'RECORD': 48, 'OwnerIndex': implementation_index}))
    header = {'HEADER': 'Protel for Windows - Schematic Library Editor Binary File Version 5.0',
              'Weight': len(records), 'MinorVersion': 2, 'UniqueID': uuid.uuid4().hex[:8].upper(),
              'FontIDCount': len(fonts), 'UseMBCS': 'T', 'IsBOC': 'T', 'SheetStyle': 9,
              # Match a new AD SchLib's editor canvas. Missing values become
              # black/off/zero rather than inheriting the user's defaults.
              'BorderOn': 'T', 'SheetNumberSpaceSize': 12, 'Display_Unit': 0,
              'CustomX': 18000, 'CustomY': 18000, 'UseCustomSheet': 'T', 'ReferenceZonesOn': 'T',
              'AreaColor': 0xffffff, 'SystemFont': 1, 'SnapGridOn': 'T', 'SnapGridSize': 10,
              'VisibleGridOn': 'T', 'VisibleGridSize': 10,
              'CompCount': 1, 'LibRef0': name, 'CompDescr0': data.get('description') or '', 'PartCount0': len(units) + 1}
    for i, (family, size) in enumerate(fonts, 1):
        header[f'FontName{i}'], header[f'Size{i}'] = family, round(size)
    section = _section(part + '_Symbol') if merged else _section(name)
    streams = {'FileHeader': _parameters(header), 'SectionKeys': _parameters({'KeyCount': 1, 'LibRef0': name, 'SectionKey0': section}),
               section + '/Data': b''.join(records), 'Storage': _parameters({'HEADER': 'Icon storage'})}
    return header, streams


def _pad_record(layer, center, size, designator, shape, rotation=0, drill=0, plated=True,
                slot_length=0, slot_angle=0, paste=0, solder=0, copper_offset=(0, 0)):
    body = bytearray(202)
    body[:13] = _common(layer)
    struct.pack_into('<iiiiiiiii', body, 13, *center, *size, *size, *size, drill)
    # The three shape bytes follow the nine coordinate fields at offset 49.
    body[49:52] = bytes([shape] * 3)
    struct.pack_into('<d', body, 52, rotation)
    body[60] = int(plated)
    body[67] = 1  # Relief connection style; values below are standard defaults.
    struct.pack_into('<ihiii', body, 68, 100000, 4, 100000, 200000, 200000)
    struct.pack_into('<ii', body, 86, paste, solder)
    body[101:103] = b'\x02\x02'  # Explicit manual paste and solder expansions.
    struct.pack_into('<I', body, 114, _v7_layer(layer))
    body[126:142], body[142:158] = uuid.uuid4().bytes_le, uuid.uuid4().bytes_le
    struct.pack_into('<ii', body, 162, 0x7fffffff, 0x7fffffff)
    body[172], body[185], body[186] = 0x1a, 1, 1
    stack = bytearray(596)
    for i in range(29):
        struct.pack_into('<i', stack, i * 4, size[0])
        struct.pack_into('<i', stack, 116 + i * 4, size[1])
        stack[232 + i] = shape
    stack[262] = 2 if slot_length else 0
    struct.pack_into('<id', stack, 263, slot_length, slot_angle)
    for i in range(32):
        struct.pack_into('<i', stack, 275 + i * 4, copper_offset[0])
        struct.pack_into('<i', stack, 403 + i * 4, copper_offset[1])
    return b'\x02' + _string_block(designator) + _string_block('') + _string_block('|&|0') + _block(b'\0') + _block(body) + _block(stack)


def _track_record(layer, a, b, width):
    body = _common(layer) + struct.pack('<iiiiihihI', *a, *b, width, 0, 0, 0, _v7_layer(layer))
    return b'\x04' + _block(body)


def _arc_record(layer, center, radius, width):
    body = _common(layer) + struct.pack('<iiiddi', *center, radius, 0., 360., width)
    return b'\x01' + _block(body)


def _region_record(layer, vertices, kind=0, pad_index=None, paste=None, solder=None):
    values = {'V7_LAYER': LAYER_NAMES.get(layer, f'MIDLAYER{layer - 1}'), 'NAME': '', 'KIND': kind,
              'SUBPOLYINDEX': -1, 'UNIONINDEX': 0, 'ARCRESOLUTION': '0.5mil',
              'ISSHAPEBASED': 'FALSE', 'CAVITYHEIGHT': '0mil'}
    if pad_index is not None:
        values['PADINDEX'] = pad_index
    for key, expansion in (('PASTEMASK', paste), ('SOLDERMASK', solder)):
        if expansion is not None:
            values[key + 'EXPANSIONMODE'] = 'Manual'
            values[key + 'EXPANSION_MANUAL'] = str(Decimal(expansion) / 10000) + 'mil'
    body = _common(layer) + bytes(5) + _parameters(values) + struct.pack('<I', len(vertices))
    body += b''.join(struct.pack('<dd', *vertex) for vertex in vertices)
    return b'\x0b' + _block(body)


def _text_record(layer, center, height, width, rotation, text, index, mirror=False):
    body = bytearray(252)
    body[:13] = _common(layer)
    struct.pack_into('<iiihdBi', body, 13, *center, height, 0, rotation, int(mirror), width)
    body[43] = 1  # TrueType; WideStrings carries the full Unicode text.
    body[46:56] = 'Arial'.encode('utf-16le')
    struct.pack_into('<i', body, 115, index)
    body[160] = 1
    struct.pack_into('<I', body, 226, _v7_layer(layer))
    return b'\x05' + _block(body) + _block(_short_string(text, replace=True))


def _pcb_header():
    """Supply legacy library editor metadata, including its numeric defaults.

These describe the library editor's grids/layers, not a physical PCB stack.
The legacy reader requires numeric fields even when their value is zero.
"""
    p = {'RECORD': 'Board', 'KIND': 'Protel_Advanced_PCB_Library', 'VERSION': '3.00',
         'TOPTYPE': 3, 'BOTTOMTYPE': 3, 'TOPCONST': '3.5', 'BOTTOMCONST': '3.5',
         'TOPHEIGHT': '0.4mil', 'BOTTOMHEIGHT': '0.4mil', 'TOPMATERIAL': 'Solder Resist',
         'BOTTOMMATERIAL': 'Solder Resist', 'LAYERSTACKSTYLE': 0, 'LAYERSETSCOUNT': 0,
         'BIGVISIBLEGRIDSIZE': 100000, 'VISIBLEGRIDSIZE': 50000,
         'SNAPGRIDSIZE': 50000, 'SNAPGRIDSIZEX': 50000, 'SNAPGRIDSIZEY': 50000,
         'ELECTRICALGRIDRANGE': '8mil', 'DISPLAYUNIT': 1, 'TOGGLELAYERS': '1' * 82,
         'CFG2D.TOGGLELAYERS': '1' * 82, 'CFG2D.CURRENTLAYER': 'TOP',
         'CFG2D.EYEDIST': 2000, 'CFG2D.PLANEDRAWMODE': 2, 'CFG2D.DISPLAYNETNAMESONTRACKS': 1,
         'CFG2D.FROMTOSDISPLAYMODE': 0, 'CFG2D.PADTYPESDISPLAYMODE': 0, 'CFG2D.SINGLELAYERMODESTATE': 0,
         'CFG2D.ORIGINMARKERCOLOR': 16777215, 'CFG2D.COMPONENTREFPOINTCOLOR': 16777215,
         'CFG2D.TOPPOSITIVESOLDERMASKALPHA': '.5', 'CFG2D.BOTTOMPOSITIVESOLDERMASKALPHA': '.5',
         'VISIBLEGRIDMULTFACTOR': 1, 'BIGVISIBLEGRIDMULTFACTOR': 5, 'CURRENT2D3DVIEWSTATE': '2D',
         'VP.LX': -5000000, 'VP.HX': 5000000, 'VP.LY': -5000000, 'VP.HY': 5000000,
         'LOOKAT.X': 0, 'LOOKAT.Y': 0, 'LOOKAT.Z': 0, 'EYEROTATION.X': 0, 'EYEROTATION.Y': 0,
         'EYEROTATION.Z': 0, 'ZOOMMULT': '.000001', 'VIEWSIZE.X': 10000000, 'VIEWSIZE.Y': 10000000,
         'EGRANGE': '8mil', 'EGMULT': 0, 'NEARDISTANCE': '1000mil'}
    for key in ('SHOWDEFAULTSETS', 'ELECTRICALGRIDENABLED', 'CFG2D.SHOWORIGINMARKER', 'CFG2D.SHOWSTATUSINFO',
                'CFG2D.SHOWPADNUMBERS', 'GRIDSNAPENABLED', 'EGENABLED', 'NEAROBJECTSENABLED', 'FAROBJECTSENABLED'):
        p[key] = 'TRUE'
    for layer in range(1, 83):
        label = LAYER_NAMES.get(layer, f'Inner {layer - 1}' if layer <= 31 else f'Layer {layer}')
        defaults = {'NAME': label, 'PREV': 1 if layer == 32 else 0, 'NEXT': 32 if layer == 1 else 0,
                    'MECHENABLED': 'TRUE' if 57 <= layer <= 72 else 'FALSE', 'COPTHICK': '1.4mil',
                    'DIELTYPE': 0, 'DIELCONST': '4.8', 'DIELHEIGHT': '12.6mil', 'DIELMATERIAL': 'FR-4'}
        p.update({f'LAYER{layer}{key}': value for key, value in defaults.items()})
    return p


@traced('export.pcblib', lambda data, part, *a, **kw: {'part': safe_part(part), 'format': 'PCBLIB'}, level='INFO')
def export_pcblib(data, part, check_cancelled=lambda: None):
    _, streams = _pcblib_component(data, part, check_cancelled)
    return compound_file(streams, check_cancelled)


def _pcblib_component(data, part, check_cancelled, *, merged=False, footprint_name=None):
    doc, head, name = _pcb_documents(data, part)
    if merged:
        name = merged_name(name, part)
    if footprint_name is not None:
        name = footprint_name
    shapes = _shapes(doc, '封装')
    ox, oy = _decimal(head.get('x', 0)), _decimal(head.get('y', 0))
    def point(px, py): return _raw(_decimal(px) - ox), _raw(oy - _decimal(py))
    records, texts, pads = [], [], 0
    for shape in shapes:
        check_cancelled()
        f = shape.split('~')
        kind = f[0]
        try:
            if kind == 'PAD':
                layer = _layer(f[6])
                if layer not in (1, 32, 74):
                    raise DownloadError('AD 焊盘需要顶层、底层或多层图层')
                copper_center = (number(f[2]), number(f[3]))
                if len(f) > 19 and f[19]:
                    center = points(f[19])[0]
                elif number(f[13], 0) and f[14]:
                    endpoints = points(f[14])
                    if len(endpoints) != 2:
                        raise DownloadError('AD 槽孔中心线无效')
                    center = tuple((a + b) / 2 for a, b in zip(*endpoints))
                    # The precise drill centre is in slot_outline; retain the
                    # independently declared copper centre and its offset.
                elif f[1] != 'POLYGON' and f[10]:
                    # Legacy pads omit hole_point and round center_x/y more
                    # coarsely than their symmetric, already-rotated outline.
                    # Recover the precise centre from that outline, as in the
                    # official relay sample, instead of introducing an offset.
                    outline = points(f[10])
                    if len(outline) >= 2:
                        center = copper_center = ((min(p[0] for p in outline) + max(p[0] for p in outline)) / 2,
                                                   (min(p[1] for p in outline) + max(p[1] for p in outline)) / 2)
                    else:
                        center = copper_center
                else:
                    center = copper_center
                size = _raw(f[4]), _raw(f[5])
                drill = _raw(_decimal(f[9] or '0') * 2)
                slot = _raw(f[13] or '0')
                if min(size) <= 0 or drill < 0 or slot < 0:
                    raise DownloadError('AD 焊盘或钻孔尺寸无效')
                rotation = number(f[11], 0)
                outline = None
                native_shape = f[1]
                if native_shape == 'POLYGON':
                    outline = pad_outline(points(f[10]), rotation, check_cancelled=check_cancelled)
                    if outline.shape:
                        native_shape = outline.shape
                        copper_center = outline.center
                        size = tuple(_raw(v) for v in outline.size)
                        rotation = outline.rotation
                        if not drill:
                            center = copper_center
                dx, dy = copper_center[0] - number(center[0]), number(center[1]) - copper_center[1]
                angle = math.radians(rotation)
                copper_offset = (_raw(math.cos(angle) * dx + math.sin(angle) * dy),
                                 _raw(-math.sin(angle) * dx + math.cos(angle) * dy))
                slot_angle = 0
                if slot:
                    hole_points = points(f[14])
                    if len(hole_points) != 2 or not drill or slot < drill:
                        raise DownloadError('AD 槽孔中心线或尺寸无效')
                    a, b = hole_points
                    slot_angle = math.degrees(math.atan2(a[1] - b[1], b[0] - a[0])) - rotation
                if f[1] not in ('RECT', 'ELLIPSE', 'OVAL', 'POLYGON'):
                    raise DownloadError('AD 封装导出暂不支持焊盘形状 ' + f[1])
                # ROTATION is the vendor's CCW angle; Y-flip maps its polygon
                # into AD's upward-positive coordinate system.
                plated_text = f[15].strip().lower()
                if plated_text not in ('y', 'yes', 'true', '1', 'n', 'no', 'false', '0'):
                    raise DownloadError('AD 焊盘镀孔属性不受支持')
                plated = plated_text in ('y', 'yes', 'true', '1')
                paste = _raw(f[17] or '0') if len(f) > 17 else 0
                solder = _raw(f[18] or '0') if len(f) > 18 else 0
                if native_shape == 'POLYGON':
                    vertices = list(outline.vertices)
                    anchor, clearance = polygon_anchor(vertices, tuple(map(number, center)), check_cancelled=check_cancelled)
                    if drill:
                        # Moving a drill to an inscribed anchor would change the
                        # physical footprint. A through-hole retains its origin.
                        if anchor != tuple(map(number, center)):
                            raise DownloadError('自定义通孔焊盘的孔中心不在铜区域内')
                    else:
                        center = anchor
                    diameter = max(drill, 1, _raw(min(clearance * 1.8, min(number(f[4]), number(f[5])) / 2)))
                    if diameter > _raw(clearance * 2):
                        raise DownloadError('自定义焊盘的孔或连接锚点超出铜区域')
                    size, copper_offset = (diameter, diameter), (0, 0)
                    # AD PADINDEX is the one-based index of the owning primitive
                    # in Data, including tracks, regions and text before the pad.
                    # A zero-based pad-only count silently attaches copper to
                    # another pin or leaves an unattached copper region.
                    pad_index = len(records) + 1
                    records.append(_pad_record(layer, point(*center), size, f[8], 1, rotation, drill,
                                               plated, slot, slot_angle, paste, solder))
                    # A multilayer custom shape needs top, inner and bottom
                    # contours. MIDLAYER1 represents the internal pad shape;
                    # MULTILAYER is not an owning custom-shape copper layer.
                    for copper_layer in ((1, 2, 32) if layer == 74 else (layer,)):
                        records.append(_region_record(copper_layer, [point(*p) for p in vertices],
                                                      pad_index=pad_index, paste=paste, solder=solder))
                else:
                    records.append(_pad_record(layer, point(*center), size, f[8],
                                            {'RECT': 2, 'OCTAGON': 3}.get(native_shape, 1),
                                            rotation, drill, plated, slot, slot_angle,
                                            paste, solder, copper_offset))
                pads += 1
            elif kind == 'HOLE':
                diameter = _raw(_decimal(f[3]) * 2)
                if diameter <= 0:
                    raise DownloadError('AD 定位孔尺寸无效')
                records.append(_pad_record(74, point(f[1], f[2]), (diameter, diameter), '', 1, drill=diameter, plated=False))
            elif kind == 'TRACK':
                layer, vertices, width = _layer(f[2]), points(f[4]), _raw(f[1])
                if len(vertices) < 2 or width <= 0:
                    raise DownloadError('AD 线段尺寸无效')
                for a, b in zip(vertices, vertices[1:]):
                    records.append(_track_record(layer, point(*a), point(*b), width))
            elif kind == 'CIRCLE':
                radius, width = _raw(f[3]), _raw(f[4])
                if radius <= 0 or width <= 0:
                    raise DownloadError('AD 圆形尺寸无效')
                if f[5] == '101' and width >= radius * 2:
                    # The official exporter emits these small solid polarity
                    # dots as disks, not thick rings with twice the diameter.
                    cx, cy, r = number(f[1]), number(f[2]), number(f[3])
                    contour = path_points(f'M{cx+r} {cy} A{r} {r} 0 1 0 {cx-r} {cy} A{r} {r} 0 1 0 {cx+r} {cy} Z',
                                          check_cancelled=check_cancelled)[0][:-1]
                    records.append(_region_record(_layer(f[5]), [point(*p) for p in contour]))
                else:
                    records.append(_arc_record(_layer(f[5]), point(f[1], f[2]), radius, width))
            elif kind == 'SOLIDREGION':
                paths = path_points(f[3], check_cancelled=check_cancelled)
                if len(paths) != 1 or len(paths[0]) < 3:
                    raise DownloadError('AD 区域暂不支持多轮廓路径')
                region_kind = {'solid': 0, 'cutout': 1, 'npth': 2}.get(f[4])
                if region_kind is None:
                    raise DownloadError('AD 区域类型不受支持：' + f[4])
                vertices = paths[0]
                if vertices[-1] == vertices[0]:
                    vertices = vertices[:-1]
                records.append(_region_record(_layer(f[1]), [point(*p) for p in vertices], region_kind))
            elif kind == 'ARC':
                layer, width = _layer(f[2]), _raw(f[1])
                for vertices in path_points(f[4], check_cancelled=check_cancelled):
                    for a, b in zip(vertices, vertices[1:]):
                        records.append(_track_record(layer, point(*a), point(*b), width))
            elif kind == 'TEXT':
                if f[13] != '1':
                    continue
                text = f[10]
                records.append(_text_record(_layer(f[7]), point(f[2], f[3]), _raw(f[9]), _raw(f[4]), number(f[5], 0),
                                            text, len(texts), f[6] in ('1', 'true')))
                texts.append(text)
            elif kind == 'RECT':
                px, py, w, h, layer = number(f[1]), number(f[2]), number(f[3]), number(f[4]), _layer(f[5])
                if w <= 0 or h <= 0:
                    raise DownloadError('AD 矩形区域尺寸无效')
                vertices = [(px, py), (px + w, py), (px + w, py + h), (px, py + h)]
                # Extended RECT records distinguish an outline from a solid
                # area (stroke width at 8, fill style at 9). Older records
                # without those fields describe solid rectangles.
                width = _raw(f[8] or '0') if len(f) > 8 else 0
                filled = len(f) <= 9 or not _unfilled(f[9])
                if width < 0 or not filled and width == 0:
                    raise DownloadError('AD 矩形轮廓线宽无效')
                if filled:
                    records.append(_region_record(layer, [point(*p) for p in vertices]))
                if width:
                    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
                        records.append(_track_record(layer, point(*a), point(*b), width))
            elif kind == 'SVGNODE':
                node = document(shape.partition('~')[2])
                if document(node.get('attrs')).get('c_etype') != 'outline3D':
                    raise DownloadError('AD 封装导出暂不支持此 SVG 图元')
                # STEP embedding/placement is separate from the 2D library export.
            else:
                raise DownloadError(f'AD 封装导出暂不支持图元 {kind}')
        except (IndexError, ValueError, TypeError, struct.error) as exc:
            raise DownloadError(f'封装 {kind} 图元数据不完整或超出范围') from exc
    if not pads:
        raise DownloadError('官方封装没有可导出的焊盘')
    section = _section(name)
    version = 'PCB 6.0 Binary Library File'
    identifier = uuid.uuid4().hex[:8].upper()
    wide = {f'ENCODEDTEXT{i}': ','.join(str(unit) for unit in struct.unpack('<' + 'H' * (len(text.encode('utf-16le')) // 2), text.encode('utf-16le')))
            for i, text in enumerate(texts)}
    streams = {
        'FileHeader': struct.pack('<I', len(version)) + _short_string(version) + struct.pack('<dI', 5.01, 8) + _short_string(identifier),
        'SectionKeys': struct.pack('<I', 1) + _string_block(name) + _string_block(section),
        'Library/Header': struct.pack('<I', 1),
        'Library/Data': _parameters(_pcb_header()) + struct.pack('<I', 1) + _string_block(name),
        section + '/Header': struct.pack('<I', len(records)),
        section + '/Data': _string_block(name) + b''.join(records),
        section + '/Parameters': _parameters({'PATTERN': name, 'HEIGHT': '0mil', 'DESCRIPTION': data.get('description') or '',
                                               'ITEMGUID': '', 'REVISIONGUID': '', 'SupplierPart': part, **_source_parameters(head)}),
        section + '/WideStrings': _parameters(wide),
    }
    for folder in ('Models', 'ModelsNoEmbed', 'Textures'):
        streams[f'Library/{folder}/Header'], streams[f'Library/{folder}/Data'] = struct.pack('<I', 0), b''
    return name, streams


def merged_name(name, part):
    """Use the footprint name, without a supplier number, within storage limits."""
    return _section(_library_name(name, part))


class MergedLibrary:
    """Compile components into one native library. Caller serializes add/build."""
    def __init__(self, format, *, merge_pcb=False):
        self.format = format
        self.merge_pcb = merge_pcb
        self.fonts = [('Times New Roman', 8)]
        self.entries = {}
        self.footprint_names = {}
        self.reused_count = 0

    @traced('export.merge_component', lambda self, data, part, *a: {'part': safe_part(part), 'format': self.format})
    def add(self, data, part, check_cancelled):
        if self.format == 'SCHLIB':
            entry = _schlib_component(data, part, check_cancelled, merged=True,
                                      merge_pcb=self.merge_pcb, fonts=self.fonts)
        else:
            entry = _pcblib_component(data, part, check_cancelled, merged=True)
        check_cancelled()
        self.entries[part] = entry

    @traced('export.merge', lambda self, parts, *a, **kw: {'format': self.format, 'count': len(parts)}, level='INFO')
    def build(self, parts, check_cancelled, *, original=None, footprint_names=None):
        entries = [self.entries[part] for part in parts]
        if not entries:
            raise DownloadError('没有可合并的元件')
        streams = {}
        if self.format == 'SCHLIB':
            from library_merge import rewrite_symbol_records, unique_name
            header = dict(entries[0][0])
            header.update(CompCount=len(entries), Weight=sum(entry[0]['Weight'] for entry in entries),
                          FontIDCount=len(self.fonts))
            keys = {'KeyCount': len(entries)}
            used_names = set()
            for index, ((metadata, component), part) in enumerate(zip(entries, parts)):
                check_cancelled()
                name = unique_name(metadata['LibRef0'], used_names, limit=255)
                used_names.add(name.casefold())
                section = next(path.split('/')[0] for path in component if path.endswith('/Data'))
                header.update({f'LibRef{index}': name, f'CompDescr{index}': metadata['CompDescr0'],
                               f'PartCount{index}': metadata['PartCount0']})
                keys.update({f'LibRef{index}': name, f'SectionKey{index}': section})
                if section + '/Data' in streams:
                    raise DownloadError('合并符号的内部名称冲突')
                streams[section + '/Data'] = rewrite_symbol_records(component[section + '/Data'],
                    name=name, footprint=(footprint_names or {}).get(part))
            for index, (family, size) in enumerate(self.fonts, 1):
                header[f'FontName{index}'], header[f'Size{index}'] = family, round(size)
            streams.update(FileHeader=_parameters(header), SectionKeys=_parameters(keys),
                           Storage=_parameters({'HEADER': 'Icon storage'}))
        else:
            from library_merge import plan_footprints
            planned, self.footprint_names, self.reused_count = plan_footprints(
                [(part, *entry) for part, entry in zip(parts, entries)], original, check_cancelled)
            entries = [(name, component) for _, name, component in planned]
            streams = {path: value for path, value in entries[0][1].items()
                       if '/' not in path or path.startswith('Library/')}
            keys = struct.pack('<I', len(entries))
            names = b''
            for name, component in entries:
                check_cancelled()
                section = next(path.split('/')[0] for path in component
                               if path.endswith('/Data') and not path.startswith('Library/'))
                if section + '/Data' in streams:
                    raise DownloadError('合并封装的内部名称冲突')
                keys += _string_block(name) + _string_block(section)
                names += _string_block(name)
                streams.update({path: value for path, value in component.items() if path.startswith(section + '/')})
            streams['SectionKeys'] = keys
            # This is the single Board record count, not the footprint count.
            # Footprints are enumerated separately after that record in Data.
            streams['Library/Header'] = struct.pack('<I', 1)
            streams['Library/Data'] = _parameters(_pcb_header()) + struct.pack('<I', len(entries)) + names
        return compound_file(streams, check_cancelled)


def export_linked_schlib(data, part, check_cancelled, *, footprint_name=None):
    """An individual symbol may still reference a merged footprint library."""
    header, streams = _schlib_component(data, part, check_cancelled, merge_pcb=True, footprint_name=footprint_name)
    streams['FileHeader'] = _parameters(header)
    return compound_file(streams, check_cancelled)


def export_linked_pcblib(data, part, check_cancelled, *, footprint_name=None):
    """Keep the standalone pair usable when also writing a combined library."""
    _, streams = _pcblib_component(data, part, check_cancelled, merged=True, footprint_name=footprint_name)
    return compound_file(streams, check_cancelled)
