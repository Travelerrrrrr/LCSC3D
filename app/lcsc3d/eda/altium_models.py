"""Native Altium 3D bodies and shared embedded resource tables.

Layout and checksum are independently checked against AD 26 open/save and
SetState_FromModel probes; see docs/3D模型放置.md for the evidence boundary.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import math
import struct
import uuid
import zlib

from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.eda_ir import Pad
from lcsc3d.models.model_export import resolved_models
from lcsc3d.models.model_geometry import euler_xyz
from lcsc3d.models.model_cache import cached_model, reuse_models

MM_PER_RAW = .00000254


def model_checksum(data):
    # AD's weighted byte sum: weights 1, 1, 2, 3, ... (not a CRC).
    return (data[0] + sum(i*byte for i,byte in enumerate(data))) & 0xffffffff if data else 0


def model_payload_identity(data):
    return cached_model(('ad-model-identity', hashlib.sha256(data).digest()),
                        lambda: _model_payload_identity(data))


def _model_payload_identity(data):
    try:
        decompressor = zlib.decompressobj()
        payload = decompressor.decompress(data,64*1024*1024+1)
        if len(payload)>64*1024*1024 or decompressor.unconsumed_tail or not decompressor.eof or decompressor.unused_data:
            raise DownloadError('AD 内嵌模型数据无效或过大')
        return hashlib.sha256(payload).digest()
    except zlib.error as exc:
        raise DownloadError('AD 内嵌模型压缩数据无效') from exc


def _pose_key(values, body=False):
    prefix = 'MODEL.3D.' if body else ''
    dz = values.get(prefix+'DZ','0').strip().lower()
    if body:
        multiplier = Decimal(10000)
        if dz.endswith('mil'):
            dz = dz[:-3]
        elif dz.endswith('mm'):
            dz,multiplier = dz[:-2],Decimal(1)/Decimal('.00000254')
        dz = str(round(Decimal(dz)*multiplier))
    return (values.get('MODELID' if body else 'ID','').casefold(),
            *(Decimal(values.get(prefix+k,'0')) for k in ('ROTX','ROTY','ROTZ')),
            Decimal(dz),int(values.get('MODEL.CHECKSUM' if body else 'CHECKSUM','0'))&0xffffffff)


def _mil(value):
    return format(Decimal(str(value))/Decimal('.0254'),'.10f') + 'mil'


def _raw(value):
    value = round(float(value)/MM_PER_RAW)
    if not -2147483648 <= value <= 2147483647:
        raise DownloadError('3D 模型放置超出 AD 坐标范围')
    return value


def _outline(box, angles, offset):
    x,y,z = map(math.radians,angles)
    cx,sx,cy,sy,cz,sz = math.cos(x),math.sin(x),math.cos(y),math.sin(y),math.cos(z),math.sin(z)
    points = [(cy*a+sy*sx*b+sy*cx*c,cx*b-sx*c,-sy*a+cy*sx*b+cy*cx*c)
              for a in (box[0],box[3]) for b in (box[1],box[4]) for c in (box[2],box[5])]
    low = [min(p[i] for p in points) for i in range(3)]
    high = [max(p[i] for p in points) for i in range(3)]
    corners = [(low[0],low[1]),(high[0],low[1]),(high[0],high[1]),(low[0],high[1])]
    return [(cz*a-sz*b+offset[0],sz*a+cz*b+offset[1]) for a,b in corners],low[2]+offset[2],high[2]+offset[2]


def _footprint_outline(component):
    """A 2D body proxy from pads; never used to move or resize the STEP model."""
    points = []
    for pad in component.footprint.items:
        if not isinstance(pad, Pad):
            continue
        angle = math.radians(float(pad.rotation))
        c, s = math.cos(angle), math.sin(angle)
        x, y = float(pad.center.x), float(pad.center.y)
        dx, dy = float(pad.size.x)/2, float(pad.size.y)/2
        points.extend((x+c*a-s*b, y+s*a+c*b) for a in (-dx,dx) for b in (-dy,dy))
    left = min((p[0] for p in points), default=-.5)
    right = max((p[0] for p in points), default=.5)
    bottom = min((p[1] for p in points), default=-.5)
    top = max((p[1] for p in points), default=.5)
    return [(left,bottom),(right,bottom),(right,top),(left,top)]


@reuse_models
def embed_models(component, streams, section, payloads, check):
    from lcsc3d.eda import altium as a
    entries, records = {}, []
    for model,asset,original in resolved_models(component,payloads):
        check()
        if model.scale != (Decimal(1),)*3:
            raise DownloadError('AD 模型必须保持 STEP 原始尺寸')
        scale = float(model.scale[0])
        rotation = [[float(model.transform[i*4+j])/scale for j in range(3)] for i in range(3)]
        angles = tuple(round(v,9) for v in euler_xyz(rotation))
        offset = tuple(_raw(model.transform[i*4+3])*MM_PER_RAW for i in range(3))
        raw = original
        digest = asset.sha256  # resolved_models already verified the original bytes.
        checksum, compressed = cached_model(('ad-step', digest),
            lambda: (model_checksum(raw), zlib.compress(raw)),
            size=lambda value: len(value[1]) + 128, check=check)
        model_id = '{' + str(uuid.uuid5(uuid.NAMESPACE_URL,'lcsc3d:step:'+digest+repr((angles,_raw(offset[2]))))).upper() + '}'
        name = digest + '.step'
        metadata = {'EMBED':'TRUE','MODELSOURCE':'Undefined','ID':model_id,
                    'ROTX':angles[0],'ROTY':angles[1],'ROTZ':angles[2],'DZ':_raw(offset[2]),
                    'CHECKSUM':checksum if checksum < 2**31 else checksum-2**32,'NAME':name}
        if model_id not in entries:
            entries[model_id] = a._parameters(metadata), compressed
        if asset.bounds:  # Preserve supplied historical IR bounds without reading geometry.
            outline,low,high = _outline(tuple(float(v)*scale for v in asset.bounds),angles,offset)
        else:
            outline = _footprint_outline(component)
            low = high = 0.  # Unknown physical heights; the embedded model supplies the solid.
        fields = {'V7_LAYER':'MECHANICAL1','NAME':'','KIND':0,'SUBPOLYINDEX':-1,'UNIONINDEX':0,
                  'ARCRESOLUTION':'0.5mil','ISSHAPEBASED':'FALSE','CAVITYHEIGHT':'0mil',
                  'STANDOFFHEIGHT':_mil(low),'OVERALLHEIGHT':_mil(high),'BODYPROJECTION':0,
                  'BODYCOLOR3D':8421504,'BODYOPACITY3D':'1.000','IDENTIFIER':'','TEXTURE':'',
                  'TEXTURECENTERX':'0mil','TEXTURECENTERY':'0mil','TEXTURESIZEX':'0mil','TEXTURESIZEY':'0mil','TEXTUREROTATION':0,
                  'MODELID':model_id,'MODEL.CHECKSUM':checksum,'MODEL.EMBED':'TRUE','MODEL.NAME':name,
                  'MODEL.2D.X':_mil(offset[0]),'MODEL.2D.Y':_mil(offset[1]),'MODEL.2D.ROTATION':0,
                  'MODEL.3D.ROTX':angles[0],'MODEL.3D.ROTY':angles[1],'MODEL.3D.ROTZ':angles[2],
                  'MODEL.3D.DZ':_mil(offset[2]),'MODEL.MODELTYPE':1}
        body = a._common(57)+bytes(5)+a._parameters(fields)+struct.pack('<I',len(outline))
        body += b''.join(struct.pack('<dd',_raw(x),_raw(y)) for x,y in outline)
        records.append(b'\x0c'+a._block(body))
    if records:
        streams[section+'/Data'] += b''.join(records)
        count, = struct.unpack('<I',streams[section+'/Header'])
        streams[section+'/Header'] = struct.pack('<I',count+len(records))
        streams['Library/Models/Header'] = struct.pack('<I',len(entries))
        streams['Library/Models/Data'] = b''.join(block for block,_ in entries.values())
        for i,(_,data) in enumerate(entries.values()):
            streams['Library/Models/'+str(i)] = data


def _model_table(streams):
    from lcsc3d.eda.library_merge import Reader,parameters
    header = streams.get('Library/Models/Header',struct.pack('<I',0))
    if len(header) != 4:
        raise DownloadError('AD 模型目录无效，原库保留')
    count, = struct.unpack('<I',header)
    if count > 10000:
        raise DownloadError('AD 模型目录过大，原库保留')
    reader = Reader(streams.get('Library/Models/Data',b''))
    result = []
    for i in range(count):
        start = reader.offset
        flag,body = reader.block()
        values = parameters(body)
        if flag or not values.get('ID'):
            raise DownloadError('AD 模型元数据无效，原库保留')
        data = streams.get('Library/Models/'+str(i))
        if values.get('EMBED','').upper() == 'TRUE' and data is None:
            raise DownloadError('AD 内嵌模型数据缺失，原库保留')
        result.append((values,reader.data[start:reader.offset],data))
    if reader.offset != len(reader.data):
        raise DownloadError('AD 模型元数据存在未知尾部，原库保留')
    return result


def _rewrite_ids(streams, mapping):
    from lcsc3d.eda import altium as a
    from lcsc3d.eda.library_merge import Reader,parameters,patch_parameters
    result = dict(streams)
    for path,data in streams.items():
        if path.startswith('Library/') or not path.endswith('/Data'):
            continue
        reader = Reader(data)
        reader.string()
        records = [data[:reader.offset]]
        while reader.offset < len(data):
            kind = reader.take(1)[0]
            blocks = [reader.block() for _ in range(6 if kind == 2 else 2 if kind == 5 else 1)]
            if kind == 12:
                flag,body = blocks[0]
                props = Reader(body[18:])
                parameter_flag,parameter_body = props.block()
                values = parameters(parameter_body)
                replacement = mapping.get(_pose_key(values,body=True))
                if replacement:
                    if flag or parameter_flag:
                        raise DownloadError('AD 模型图元无法重映射')
                    body = body[:18]+patch_parameters(parameter_body,{'MODELID':replacement})+body[18+props.offset:]
                    blocks[0] = flag,body
            records.append(bytes([kind])+b''.join(a._block(body,flag) for flag,body in blocks))
        result[path] = b''.join(records)
    return result


def aggregate_models(base, components):
    """Preserve old indices and combine every accepted footprint's resources."""
    from lcsc3d.eda.library_merge import Reader,patch_parameters
    entries = _model_table(base)
    known = {}
    for item in entries:
        known.setdefault(_pose_key(item[0]),[]).append(item)
    used_ids = {v['ID'].casefold() for v,_,_ in entries}
    updated = []
    for component in components:
        mapping = {}
        for values,block,data in _model_table(component):
            pose = _pose_key(values)
            matches = known.get(pose,[])
            if any((old_data is None and data is None) or (old_data is not None and data is not None and
                   model_payload_identity(old_data)==model_payload_identity(data)) for old,_,old_data in matches):
                continue
            values = dict(values)
            if values['ID'].casefold() in used_ids:
                old_id = values['ID']
                seed = hashlib.sha256(block+(data or b'')).hexdigest()
                number = 0
                while True:
                    fresh = '{'+str(uuid.uuid5(uuid.NAMESPACE_URL,'lcsc3d:merge:'+seed+str(number))).upper()+'}'
                    if fresh.casefold() not in used_ids: break
                    number += 1
                values['ID'] = fresh
                mapping[pose] = fresh
                flag,body = Reader(block).block()
                block = patch_parameters(body,{'ID':fresh})
            item = (values,block,data)
            entries.append(item)
            known.setdefault(_pose_key(values),[]).append(item)
            used_ids.add(values['ID'].casefold())
        updated.append(_rewrite_ids(component,mapping) if mapping else component)
    result = dict(base)
    result['Library/Models/Header'] = struct.pack('<I',len(entries))
    result['Library/Models/Data'] = b''.join(block for _,block,_ in entries)
    for i,(_,_,data) in enumerate(entries):
        if data is not None:
            result['Library/Models/'+str(i)] = data
    return result,updated
