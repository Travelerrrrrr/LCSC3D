"""Bounded, cancellable STEP inspection in an isolated CAD worker.

Only this worker imports OpenCascade. Source models remain byte-identical;
uniformly scaled copies are produced only for target formats without scaling.
All work files are below the application's temporary directory.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

from app_paths import temporary_directory
from errors import DownloadError

_LOCK = threading.Lock()
_WORKERS = threading.Semaphore(2)
_CACHE = OrderedDict()
_CACHE_BYTES = 64 * 1024 * 1024


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


def transform_bounds(bounds, matrix):
    points = [tuple(sum(float(matrix[row*4+j])*p[j] for j in range(3)) + float(matrix[row*4+3])
                    for row in range(3))
              for p in ((x,y,z) for x in (bounds[0], bounds[3]) for y in (bounds[1], bounds[4]) for z in (bounds[2], bounds[5]))]
    return tuple(min(p[i] for p in points) for i in range(3)) + tuple(max(p[i] for p in points) for i in range(3))


def _cached(key):
    with _LOCK:
        result = _CACHE.get(key)
        if result is not None:
            _CACHE.move_to_end(key)
        return result


def _remember(key, result):
    with _LOCK:
        _CACHE[key] = result
        _CACHE.move_to_end(key)
        while len(_CACHE) > 64 or sum(len(v) if isinstance(v, bytes) else 2048 for v in _CACHE.values()) > _CACHE_BYTES:
            _CACHE.popitem(last=False)


def _run(step, obj, scale, check):
    if not step or len(step) > 64*1024*1024 or b'ISO-10303-21' not in step[:2048]:
        raise DownloadError('STEP 文件无效或超出模型处理范围')
    if obj is not None and len(obj.encode('utf-8')) > 96*1024*1024:
        raise DownloadError('OBJ 文件超出模型处理范围')
    while not _WORKERS.acquire(timeout=.1):
        check()
    try:
        check()
        with tempfile.TemporaryDirectory(prefix='model-geometry-', dir=temporary_directory()) as directory:
            root = Path(directory)
            (root/'input.step').write_bytes(step)
            if obj is not None:
                (root/'input.obj').write_text(obj, encoding='utf-8')
            request = root/'request.json'
            request.write_text(json.dumps({'scale': scale, 'inspect': obj is not None}), encoding='utf-8')
            args = ([sys.executable, '--model-geometry', str(request)] if getattr(sys, 'frozen', False)
                    else [sys.executable, str(Path(__file__).resolve()), '--model-geometry', str(request)])
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=environment, creationflags=flags)
            deadline = time.monotonic() + 120
            try:
                while process.poll() is None:
                    check()
                    if time.monotonic() > deadline:
                        raise DownloadError('模型几何处理超时，已跳过此模型')
                    try:
                        process.wait(timeout=.1)
                    except subprocess.TimeoutExpired:
                        pass
                check()
                report = root/'result.json'
                if not report.is_file():
                    raise DownloadError('模型几何处理失败，已跳过此模型')
                value = json.loads(report.read_text(encoding='utf-8'))
                if not value.get('success'):
                    raise DownloadError(value.get('error', '模型几何处理失败'))
                return (root/'scaled.step').read_bytes() if scale is not None else value
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    finally:
        _WORKERS.release()


def inspect_models(step, obj, check=lambda: None):
    check()
    key = ('inspect', hashlib.sha256(step).hexdigest(), hashlib.sha256(obj.encode('utf-8')).hexdigest())
    result = _cached(key)
    if result is None:
        result = _run(step, obj, None, check)
        _remember(key, result)
    check()
    return result


def scaled_step(step, factor, check=lambda: None):
    factor = float(factor)
    if not math.isfinite(factor) or not 0 < factor <= 10000:
        raise DownloadError('模型缩放比例无效')
    if factor == 1:
        return step
    key = ('scale', hashlib.sha256(step).hexdigest(), factor)
    result = _cached(key)
    if result is None:
        result = _run(step, None, factor, check)
        _remember(key, result)
    check()
    return result


def _bounds(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise DownloadError('STEP 没有可用的实体范围')
    low, high = box.CornerMin(), box.CornerMax()
    values = (low.X(),low.Y(),low.Z(),high.X(),high.Y(),high.Z())
    if not all(math.isfinite(v) and abs(v) < 1e7 for v in values) or any(values[i+3] <= values[i] for i in range(3)):
        raise DownloadError('STEP 实体尺寸无效')
    return values


def _mesh(shape):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRep import BRep_Tool
    mesher = BRepMesh_IncrementalMesh(shape, .025, False, .25, False)
    mesher.Perform()
    vertices, triangles = [], []
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face(faces.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is not None:
            start = len(vertices)
            transformation = location.Transformation()
            for i in range(1, mesh.NbNodes()+1):
                p = mesh.Node(i).Transformed(transformation)
                vertices.append((p.X(), p.Y(), p.Z()))
            for i in range(1, mesh.NbTriangles()+1):
                triangles.append(tuple(start + j - 1 for j in mesh.Triangle(i).Get()))
            if len(vertices) > 750000 or len(triangles) > 1000000:
                raise DownloadError('STEP 网格过大，无法可靠核对模型')
        faces.Next()
    if not triangles:
        raise DownloadError('STEP 没有可核对的表面')
    return vertices, triangles


def _distance_squared(p, a, b, c):
    def sub(u,v): return tuple(x-y for x,y in zip(u,v))
    def dot(u,v): return sum(x*y for x,y in zip(u,v))
    ab, ac, ap = sub(b,a), sub(c,a), sub(p,a)
    d1, d2 = dot(ab,ap), dot(ac,ap)
    if d1 <= 0 and d2 <= 0: return dot(ap,ap)
    bp = sub(p,b)
    d3, d4 = dot(ab,bp), dot(ac,bp)
    if d3 >= 0 and d4 <= d3: return dot(bp,bp)
    vc = d1*d4-d3*d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1/(d1-d3) if d1 != d3 else 0.
        return sum((ap[i]-v*ab[i])**2 for i in range(3))
    cp = sub(p,c)
    d5, d6 = dot(ab,cp), dot(ac,cp)
    if d6 >= 0 and d5 <= d6: return dot(cp,cp)
    vb = d5*d2-d1*d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2/(d2-d6) if d2 != d6 else 0.
        return sum((ap[i]-w*ac[i])**2 for i in range(3))
    va = d3*d6-d5*d4
    if va <= 0 and d4-d3 >= 0 and d5-d6 >= 0:
        den = d4-d3+d5-d6
        w = (d4-d3)/den if den else 0.
        return sum((bp[i]-w*(c[i]-b[i]))**2 for i in range(3))
    den = va+vb+vc
    if abs(den) < 1e-30: return min(dot(ap,ap),dot(bp,bp),dot(cp,cp))
    return sum((ap[i]-(vb*ab[i]+vc*ac[i])/den)**2 for i in range(3))


def _bvh(vertices, triangles):
    boxes = [(tuple(min(vertices[j][i] for j in t) for i in range(3)),
              tuple(max(vertices[j][i] for j in t) for i in range(3))) for t in triangles]
    def build(indices):
        low = tuple(min(boxes[j][0][i] for j in indices) for i in range(3))
        high = tuple(max(boxes[j][1][i] for j in indices) for i in range(3))
        if len(indices) <= 16: return low,high,indices,None
        axis = max(range(3), key=lambda i:high[i]-low[i])
        indices.sort(key=lambda j:boxes[j][0][axis]+boxes[j][1][axis])
        middle = len(indices)//2
        return low,high,build(indices[:middle]),build(indices[middle:])
    root = build(list(range(len(triangles))))
    def distance(p):
        best = math.inf
        stack = [root]
        while stack:
            low,high,left,right = stack.pop()
            bound = sum(max(low[i]-p[i],0.,p[i]-high[i])**2 for i in range(3))
            if bound >= best: continue
            if right is None:
                for j in left:
                    a,b,c = (vertices[k] for k in triangles[j])
                    best = min(best,_distance_squared(p,a,b,c))
            else:
                children = sorted((left,right), key=lambda n:sum(max(n[0][i]-p[i],0.,p[i]-n[1][i])**2 for i in range(3)), reverse=True)
                stack.extend(children)
        return math.sqrt(best)
    return distance


def _compatible(step_vertices, step_faces, step_box, obj):
    ov = obj.vertices
    ob = tuple(min(p[i] for p in ov) for i in range(3)) + tuple(max(p[i] for p in ov) for i in range(3))
    extents = [ob[i+3]-ob[i] for i in range(3)]
    if min(extents) <= 0:
        raise DownloadError('OBJ 模型尺寸无效')
    tolerance = max(.06, math.sqrt(sum(d*d for d in extents))*.003)
    if any(abs(step_box[i+3]-step_box[i]-extents[i]) > tolerance*2 for i in range(3)):
        return ob,False,{'reason':'bounds_mismatch'}
    def normalize(vertices, box):
        anchor = ((box[0]+box[3])/2,(box[1]+box[4])/2,box[2])
        return [tuple(p[i]-anchor[i] for i in range(3)) for p in vertices]
    sv,ov = normalize(step_vertices,step_box),normalize(ov,ob)
    of = [(f[1][0],f[1][i],f[1][i+1]) for f in obj.faces for i in range(1,len(f[1])-1)]
    def samples(vertices, faces):
        stride = max(1,len(vertices)//384)
        result = vertices[::stride][:384]
        result += [op(vertices,key=lambda p:p[i]) for i in range(3) for op in (min,max)]
        result += [tuple(sum(vertices[j][i] for j in f)/3 for i in range(3)) for f in faces[::max(1,len(faces)//128)][:128]]
        return result
    distances = []
    for vertices,faces,target,target_faces in ((sv,step_faces,ov,of),(ov,of,sv,step_faces)):
        nearest = _bvh(target,target_faces)
        distances.extend(nearest(p) for p in samples(vertices,faces))
    distances.sort()
    p95,maximum = distances[int(.95*(len(distances)-1))],distances[-1]
    return ob,p95 <= tolerance and maximum <= tolerance*4,{'p95_mm':p95,'max_mm':maximum,'tolerance_mm':tolerance}


def _scale_units(data, factor):
    """Scale physical length units without rewriting assembly/style entities.

    Only direct SI length contexts are supported. Existing conversion-based
    contexts must be rejected: nested conversions are not read consistently
    by CAD engines and a correct outer bounding box can conceal a wrong part.
    """
    records = []
    entity_start = re.compile(rb'#(\d+)\s*=\s*')
    i, size = 0,len(data)
    while i<size:
        if data[i:i+2] == b'/*':
            end = data.find(b'*/',i+2)
            if end<0: raise DownloadError('STEP 注释未闭合')
            i=end+2
        elif data[i:i+1] == b"'":
            i+=1
            while i<size:
                if data[i:i+2] == b"''": i+=2
                elif data[i:i+1] == b"'": i+=1; break
                else:i+=1
        elif data[i:i+1] == b'#' and (match := entity_start.match(data,i)):
            start,body_start,number = i,match.end(),int(match[1])
            i=body_start
            quoted=False
            while i<size:
                if not quoted and data[i:i+2]==b'/*':
                    end=data.find(b'*/',i+2)
                    if end<0:raise DownloadError('STEP 注释未闭合')
                    i=end+2;continue
                if data[i:i+1]==b"'":
                    if quoted and data[i:i+2]==b"''":i+=2;continue
                    quoted=not quoted
                if not quoted and data[i:i+1]==b';':
                    i+=1
                    records.append((number,start,i,data[body_start:i-1]))
                    break
                i+=1
        else:i+=1
    if not records:
        raise DownloadError('STEP 实体记录缺失')
    def code_only(body):
        masked=bytearray(body)
        i=0
        while i<len(body):
            if body[i:i+2]==b'/*':
                end=body.find(b'*/',i+2)
                if end<0:raise DownloadError('STEP 注释未闭合')
                end+=2
                masked[i:end]=b' '*(end-i)
                i=end
            elif body[i:i+1]==b"'":
                start=i
                i+=1
                while i<len(body):
                    if body[i:i+2]==b"''":i+=2
                    elif body[i:i+1]==b"'":i+=1;break
                    else:i+=1
                masked[start:i]=b' '*(i-start)
            else:i+=1
        return bytes(masked)
    unit_pattern = re.compile(rb'SI_UNIT\s*\(\s*(?:\$|\.[A-Z]+\.)\s*,\s*\.METRE\.\s*\)',re.I)
    units=[]
    for n,start,end,body in records:
        code=code_only(body)
        if not re.search(rb'\bLENGTH_UNIT\s*\(\s*\)',code,re.I):continue
        remainder=unit_pattern.sub(b'',code)
        remainder=re.sub(rb'(?:LENGTH_UNIT\s*\(\s*\)|NAMED_UNIT\s*\(\s*\*\s*\))',b'',remainder,flags=re.I)
        if re.sub(rb'[\s()]',b'',remainder) or not unit_pattern.search(code):
            raise DownloadError('STEP 包含非 SI 或混合转换长度单位，无法可靠缩放，已跳过关联')
        units.append((n,start,end,body))
    if not units:
        raise DownloadError('STEP 长度单位不支持可靠缩放，已跳过关联')
    identifier = max(r[0] for r in records)+1
    dimension = identifier
    additions = [f'#{dimension}=DIMENSIONAL_EXPONENTS(1.,0.,0.,0.,0.,0.,0.);\n'.encode('ascii')]
    identifier+=1
    changes = []
    factor_text = format(factor,'.17g').replace('e','E')
    if '.' not in factor_text and 'E' not in factor_text: factor_text+='.'
    for number,start,end,body in units:
        base,measure = identifier,identifier+1
        identifier+=2
        additions.append(f'#{base}='.encode('ascii')+body+b';\n')
        additions.append(f'#{measure}=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE({factor_text}),#{base});\n'.encode('ascii'))
        replacement = unit_pattern.sub(f"CONVERSION_BASED_UNIT('LCSC3D scaled length',#{measure})".encode('ascii'),body)
        replacement,count = re.subn(rb'NAMED_UNIT\s*\(\s*\*\s*\)',f'NAMED_UNIT(#{dimension})'.encode('ascii'),replacement,flags=re.I)
        if count!=1:
            raise DownloadError('STEP 长度单位定义无法安全缩放')
        # Complex STEP entity components use alphabetical entity-name order.
        replacement = (f"(CONVERSION_BASED_UNIT('LCSC3D scaled length',#{measure}) "
                       f'LENGTH_UNIT() NAMED_UNIT(#{dimension}))').encode('ascii')
        changes.append((start,end,f'#{number}='.encode('ascii')+replacement+b';'))
    data_end = data.upper().rfind(b'ENDSEC;')
    if data_end<records[-1][2]:
        raise DownloadError('STEP 数据段未闭合')
    changes.append((data_end,data_end,b''.join(additions)))
    result,cursor = [],0
    for start,end,replacement in sorted(changes):
        result.extend((data[cursor:start],replacement))
        cursor=end
    result.append(data[cursor:])
    return b''.join(result)


def worker(request):
    root = Path(request).resolve().parent
    base = Path(temporary_directory()).resolve()
    if not root.is_relative_to(base) or root == base:
        return 2
    result = {'success':False}
    try:
        args = json.loads(Path(request).read_text(encoding='utf-8'))
        from OCP.IFSelect import IFSelect_RetDone
        if args.get('scale') is not None:
            from OCP.STEPControl import STEPControl_Reader
            factor = float(args['scale'])
            if not math.isfinite(factor) or not 0<factor<=10000:
                raise DownloadError('模型缩放比例无效')
            scaled = _scale_units((root/'input.step').read_bytes(),factor)
            (root/'scaled.step').write_bytes(scaled)
            boxes=[]
            for name in ('input.step','scaled.step'):
                reader=STEPControl_Reader()
                if reader.ReadFile(str(root/name))!=IFSelect_RetDone:
                    raise DownloadError('STEP 缩放副本无法读取')
                reader.SetSystemLengthUnit(1.)
                if not reader.TransferRoots():raise DownloadError('STEP 缩放副本没有实体')
                boxes.append(_bounds(reader.OneShape()))
            if any(abs(a*factor-b)>max(.00001,abs(a*factor)*1e-8) for a,b in zip(*boxes)):
                raise DownloadError('STEP 缩放副本未通过几何核对')
            result = {'success':True}
        else:
            from OCP.STEPControl import STEPControl_Reader
            from model3d import read_obj
            reader = STEPControl_Reader()
            if reader.ReadFile(str(root/'input.step')) != IFSelect_RetDone:
                raise DownloadError('STEP 无法读取，已跳过此模型')
            reader.SetSystemLengthUnit(1.)
            if not reader.TransferRoots():
                raise DownloadError('STEP 没有可用实体')
            shape = reader.OneShape()
            box = _bounds(shape)
            vertices,triangles = _mesh(shape)
            obj = read_obj((root/'input.obj').read_text(encoding='utf-8-sig'))
            ob,compatible,metrics = _compatible(vertices,triangles,box,obj)
            result = {'success':True,'bounds':box,'obj_bounds':ob,'compatible':compatible,'comparison':metrics}
    except Exception as exc:
        result['error'] = str(exc) if isinstance(exc,DownloadError) else '模型几何处理失败，无法确认 STEP 与 OBJ 坐标一致'
    (root/'result.json').write_text(json.dumps(result,allow_nan=False),encoding='utf-8')
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(worker(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == '--model-geometry' else 2)
