"""Read official model associations and OBJ geometry for local preview.

Coordinates stay in their original millimetres. Downloads preserve official
STEP/OBJ files; this module does not convert EDA library formats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
from typing import Callable

from errors import DownloadError


@dataclass(frozen=True)
class ModelReference:
    name: str
    uuid: str


def document(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise DownloadError('器件文档不是有效的 JSON') from exc
    return value if isinstance(value, dict) else {}


def model_reference(component: dict) -> ModelReference | None:
    """Read the footprint's explicit 3D node, then its legacy head link."""
    package = document(component.get('packageDetail'))
    data = document(package.get('dataStr'))
    head = document(data.get('head'))
    params = document(head.get('c_para'))
    shapes = data.get('shape') or []
    if not isinstance(shapes, list):
        raise DownloadError('封装的模型关联格式不受支持')
    for shape in shapes:
        if not isinstance(shape, str) or not shape.startswith('SVGNODE~'):
            continue
        node = document(shape.partition('~')[2])
        attrs = document(node.get('attrs'))
        if attrs.get('c_etype') not in (None, 'outline3D') or not attrs.get('uuid'):
            continue
        return _reference(attrs['uuid'], attrs.get('title') or params.get('3DModel') or package.get('title'))
    if head.get('uuid_3d'):
        return _reference(head['uuid_3d'], params.get('3DModel') or package.get('title'))
    return None


def _reference(uuid, name):
    if not isinstance(uuid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uuid):
        raise DownloadError('3D 模型编号不受支持')
    return ModelReference(str(name or 'model').strip() or 'model', uuid)


@dataclass
class Material:
    diffuse: tuple[float, float, float] = (0.55, 0.58, 0.62)
    specular: tuple[float, float, float] = (0.2, 0.2, 0.2)
    shininess: float = 0.25


@dataclass
class Mesh:
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    materials: dict[str, Material] = field(default_factory=lambda: {'': Material()})

    def preview_payload(self):
        names = list(self.materials)
        indices = {name: index for index, name in enumerate(names)}
        return {
            'vertices': self.vertices,
            'faces': [[indices[name], *face] for name, face in self.faces],
            'materials': [list(self.materials[name].diffuse) for name in names],
        }


def read_obj(raw: str, check_cancelled: Callable = lambda: None) -> Mesh:
    """Read inline MTLs and v/vt/vn faces, including relative vertex indices.

EasyEDA embeds newmtl/endmtl records in OBJ and writes d=0 even for opaque
parts. Ignore that vendor field so pins and bodies remain visible. External
mtllib references and textures are never fetched.
"""
    mesh = Mesh()
    active = ''
    defining = None
    for number, original in enumerate(raw.splitlines(), 1):
        if number % 1024 == 1:
            check_cancelled()
        line = original.partition('#')[0].strip()
        if not line:
            continue
        # Tabs are legal separators too.
        fields = line.split()
        keyword = fields[0]
        tail = line[len(keyword):].strip()
        try:
            if keyword == 'v':
                if len(fields) < 4:
                    raise ValueError('顶点需要三个坐标')
                vertex = tuple(float(value) for value in fields[1:4])
                if not all(math.isfinite(value) and abs(value) <= 1e9 for value in vertex):
                    raise ValueError('顶点坐标无效')
                mesh.vertices.append(vertex)
                if len(mesh.vertices) > 1_000_000:
                    raise ValueError('模型顶点过多')
            elif keyword == 'newmtl':
                if not tail:
                    raise ValueError('材质名称缺失')
                defining = tail
                mesh.materials.setdefault(tail, Material())
            elif keyword == 'endmtl':
                defining = None
            elif keyword in ('Kd', 'Ks') and defining is not None:
                if len(fields) < 4:
                    raise ValueError('材质颜色无效')
                color = tuple(float(value) for value in fields[1:4])
                if not all(math.isfinite(value) for value in color):
                    raise ValueError('材质颜色无效')
                color = tuple(max(0.0, min(1.0, value)) for value in color)
                setattr(mesh.materials[defining], 'diffuse' if keyword == 'Kd' else 'specular', color)
            elif keyword == 'Ns' and defining is not None:
                value = float(fields[1])
                if not math.isfinite(value):
                    raise ValueError('材质光泽无效')
                mesh.materials[defining].shininess = max(0.0, min(1.0, value / 1000))
            elif keyword == 'usemtl':
                active = tail
                mesh.materials.setdefault(active, Material())
            elif keyword == 'f':
                if len(fields) < 4 or len(fields) > 1025:
                    raise ValueError('面需要至少三个顶点且不能超过 1024 个')
                face = []
                for token in fields[1:]:
                    index = int(token.split('/')[0])
                    index = index - 1 if index > 0 else len(mesh.vertices) + index if index < 0 else -1
                    if not 0 <= index < len(mesh.vertices):
                        raise ValueError('面引用了不存在的顶点')
                    face.append(index)
                mesh.faces.append((active, tuple(face)))
                if len(mesh.faces) > 1_000_000:
                    raise ValueError('模型面数过多')
        except (ValueError, IndexError) as exc:
            raise DownloadError(f'OBJ 第 {number} 行：{exc}') from exc
    check_cancelled()
    if not mesh.vertices or not mesh.faces:
        raise DownloadError('OBJ 没有可显示的网格')
    return mesh
