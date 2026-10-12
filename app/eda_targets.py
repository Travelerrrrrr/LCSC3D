"""Explicit target capability contracts; adding IR features grants no implicit support."""
from dataclasses import dataclass

from eda_ir import Pin, Pad, Via, require_domain, check_via
from errors import DownloadError, EdaError


@dataclass(frozen=True)
class TargetCapabilities:
    name: str
    format_versions: tuple[str, ...]
    features: frozenset[str]
    symbol_graphics: frozenset[str]
    footprint_graphics: frozenset[str]
    pad_shapes: frozenset[str]
    append_existing: bool = False
    model_placement: bool = False
    mechanical_footprints: bool = True

    def require(self, component, domain):
        value = require_domain(component, domain)
        if domain == 'footprint' and not self.model_placement and any(m.transform for m in component.models):
            raise DownloadError(self.name + ' 不支持模型放置')
        required = set(component.required_features)
        if domain == 'symbol' and len(value.units) > 1:
            required.add('symbol-units')
        if domain == 'footprint' and any(isinstance(i, Pad) and
                (i.drill or i.copper_center != i.center) for i in value.items):
            required.add('pad-copper-drill')
        if domain == 'footprint' and any(isinstance(i, Via) for i in value.items):
            required.add('through-vias')
        missing = required - self.features
        if missing:
            raise DownloadError(self.name + ' 不支持 IR 能力：' + ', '.join(sorted(missing)))
        if domain == 'symbol':
            numbers = [unit.number for unit in value.units]
            if not numbers or len(numbers) > 32766 or min(numbers) < 1 or len(set(numbers)) != len(numbers):
                raise DownloadError('符号单元编号无效或重复')
            if not any(isinstance(i, Pin) for u in value.units for i in u.items):
                raise DownloadError('官方符号没有可导出的引脚')
            items = (i for u in value.units for i in u.items)
        else:
            if not any(isinstance(i, Pad) and (self.mechanical_footprints or not i.mechanical_hole) for i in value.items):
                raise DownloadError('官方封装没有可导出的焊盘')
            items = iter(value.items)
        kinds = self.symbol_graphics if domain == 'symbol' else self.footprint_graphics
        for item in items:
            if isinstance(item, Pin):
                continue
            if isinstance(item, Via):
                check_via(item)
                continue
            if isinstance(item, Pad):
                if item.shape not in self.pad_shapes:
                    raise DownloadError(self.name + ' 不支持焊盘形状：' + item.shape)
            elif item.kind not in kinds:
                primitive = item.source_kind or item.kind
                raise EdaError(self.name + ' 不支持' + ('符号' if domain == 'symbol' else '封装') + '图元：' + primitive,
                    code='unsupported_primitive', domain=domain, primitive=primitive, source_id=item.source_id)
        return value


ALTIUM = TargetCapabilities(
    'AD', ('SchLib binary 5.0', 'PcbLib binary 6.0'),
    frozenset({'local-mm-y-up', 'symbol-units', 'pad-copper-drill', 'through-vias', 'model-placement', 'model-direct-placement', 'model-source-anchor'}),
    frozenset({'rect', 'ellipse', 'polyline', 'polygon', 'path', 'text'}),
    frozenset({'rect', 'ellipse', 'polyline', 'path', 'region', 'text'}),
    frozenset({'rect', 'ellipse', 'oval', 'octagon', 'polygon'}), append_existing=True, model_placement=True, mechanical_footprints=False)

KICAD = TargetCapabilities(
    'KiCad 8+', ('symbol 20211014', 'footprint 20240108', 'footprint 20241229 (embedded models, KiCad 9+)'),
    frozenset({'local-mm-y-up', 'symbol-units', 'pad-copper-drill', 'through-vias', 'model-placement', 'model-direct-placement', 'model-source-anchor'}),
    frozenset({'rect', 'ellipse', 'polyline', 'polygon', 'path', 'text'}),
    frozenset({'rect', 'ellipse', 'polyline', 'polygon', 'path', 'region', 'text'}),
    frozenset({'rect', 'ellipse', 'oval', 'octagon', 'polygon'}), model_placement=True)
