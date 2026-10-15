"""Export official EasyEDA geometry as native SchLib/PcbLib files.

CFB storage is supplied by Windows. Record framing and coordinate conventions
were checked against AltiumSharp; see licenses/THIRD-PARTY.md for provenance.
Unsupported input raises DownloadError before any output is committed.
"""
from __future__ import annotations

from lcsc3d.core.app_logging import traced, safe_part

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import struct
import uuid
import zlib

from lcsc3d.eda.compound_storage import compound_file
from lcsc3d.core.errors import DownloadError

SOURCE = 'JLCEDA/EasyEDA Official Library'
HOMEPAGES = 'https://lceda.cn/ ; https://easyeda.com/'
MODEL_LAYERS = tuple(range(1, 33))
LAYER_NAMES = {1: 'TOP', 32: 'BOTTOM', 33: 'TOPOVERLAY', 34: 'BOTTOMOVERLAY',
               35: 'TOPPASTE', 36: 'BOTTOMPASTE', 37: 'TOPSOLDER', 38: 'BOTTOMSOLDER',
               56: 'KEEPOUT', 74: 'MULTILAYER', **{n: f'MECHANICAL{n - 56}' for n in range(57, 73)}}


def validate_model_layer(value):
    if type(value) is not int or value not in MODEL_LAYERS:
        raise DownloadError('请选择 Mechanical 1–32 作为 AD 3D 模型机械层')
    return value


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


@traced('export.schlib', lambda data, part, *a, **kw: {'part': safe_part(part), 'format': 'SCHLIB'}, level='INFO')
def export_schlib(data, part, check_cancelled=lambda: None, *, symbol_style='jlc'):
    header, streams = _schlib_component(data, part, check_cancelled, symbol_style=symbol_style)
    streams['FileHeader'] = _parameters(header)
    return compound_file(streams, check_cancelled)


def _schlib_component(data, part, check_cancelled, *, merged=False, merge_pcb=False, fonts=None, footprint_name=None,
                     symbol_style='jlc'):
    from lcsc3d.eda.easyeda_import import parse_component
    from lcsc3d.eda.altium_ir import schematic
    return schematic(parse_component(data, part, check_cancelled, domains=('symbol',)), check_cancelled,
                     merged=merged, merge_pcb=merge_pcb, fonts=fonts, footprint_name=footprint_name,
                     symbol_style=symbol_style)


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


def _via_record(center, diameter, drill):
    # PCB 6.0 simple via: common header, XY, land diameter, hole diameter, span.
    body = _common(74) + struct.pack('<iiiiBB', *center, diameter, drill, 1, 32)
    return b'\x03' + _block(body)


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


@traced('export.pcblib', lambda data, part, *a, **kw: {
    'part': safe_part(part), 'format': 'PCBLIB', 'ad_3d_layer': kw.get('model_layer', 1)}, level='INFO')
def export_pcblib(data, part, check_cancelled=lambda: None, *, model_assets=None, model_layer=1):
    _, streams = _pcblib_component(data, part, check_cancelled, model_assets=model_assets, model_layer=model_layer)
    return compound_file(streams, check_cancelled)


def _pcblib_component(data, part, check_cancelled, *, merged=False, footprint_name=None, model_assets=None, model_layer=1):
    from lcsc3d.eda.easyeda_import import parse_component
    from lcsc3d.eda.altium_ir import pcb
    return pcb(parse_component(data, part, check_cancelled, domains=('footprint',)), check_cancelled,
               merged=merged, footprint_name=footprint_name, model_assets=model_assets, model_layer=model_layer)


def merged_name(name, part):
    """Use the footprint name, without a supplier number, within storage limits."""
    return _section(_library_name(name, part))


class MergedLibrary:
    """Compile components into one native library. Caller serializes add/build."""
    def __init__(self, format, *, merge_pcb=False, symbol_style='jlc', model_layer=1):
        self.format = format
        self.merge_pcb = merge_pcb
        self.symbol_style = symbol_style
        self.model_layer = validate_model_layer(model_layer)
        self.fonts = [('Times New Roman', 8)]
        self.entries = {}
        self.footprint_names = {}
        self.reused_count = 0

    @traced('export.merge_component', lambda self, data, part, *a, **kw: {'part': safe_part(part), 'format': self.format})
    def add(self, data, part, check_cancelled, *, model_assets=None):
        if self.format == 'SCHLIB':
            entry = _schlib_component(data, part, check_cancelled, merged=True,
                                      merge_pcb=self.merge_pcb, fonts=self.fonts, symbol_style=self.symbol_style)
        else:
            entry = _pcblib_component(data, part, check_cancelled, merged=True, model_assets=model_assets,
                                      model_layer=self.model_layer)
        check_cancelled()
        self.entries[part] = entry

    @traced('export.merge', lambda self, parts, *a, **kw: {'format': self.format, 'count': len(parts)}, level='INFO')
    def build(self, parts, check_cancelled, *, original=None, footprint_names=None):
        entries = [self.entries[part] for part in parts]
        if not entries:
            raise DownloadError('没有可合并的元件')
        streams = {}
        if self.format == 'SCHLIB':
            from lcsc3d.eda.library_merge import rewrite_symbol_records, unique_name
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
            from lcsc3d.eda.library_merge import plan_footprints
            planned, self.footprint_names, self.reused_count = plan_footprints(
                [(part, *entry) for part, entry in zip(parts, entries)], original, check_cancelled)
            entries = [(name, component) for _, name, component in planned]
            streams = {path: value for path, value in entries[0][1].items()
                       if '/' not in path or path.startswith('Library/')}
            from lcsc3d.eda.altium_models import aggregate_models
            streams, model_components = aggregate_models(streams, [component for _,component in entries])
            entries = [(name,component) for (name,_),component in zip(entries,model_components)]
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


def export_linked_schlib(data, part, check_cancelled, *, footprint_name=None, symbol_style='jlc'):
    """An individual symbol may still reference a merged footprint library."""
    header, streams = _schlib_component(data, part, check_cancelled, merge_pcb=True,
                                       footprint_name=footprint_name, symbol_style=symbol_style)
    streams['FileHeader'] = _parameters(header)
    return compound_file(streams, check_cancelled)


def export_linked_pcblib(data, part, check_cancelled, *, footprint_name=None, model_assets=None, model_layer=1):
    """Keep the standalone pair usable when also writing a combined library."""
    _, streams = _pcblib_component(data, part, check_cancelled, merged=True, footprint_name=footprint_name,
                                  model_assets=model_assets, model_layer=model_layer)
    return compound_file(streams, check_cancelled)
