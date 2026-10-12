"""Place original-size STEP resources using source positions and rotations."""
from dataclasses import replace
import hashlib
import math

from eda_ir import D, Pad, ModelAsset, Diagnostic
from errors import DownloadError, Cancelled
from model3d import ModelReference
from model_geometry import step_bounds, rotation_zxy, transform_bounds
from app_logging import record_error


def model_diagnostic(severity, code, message, model=None):
    return Diagnostic('model3d',severity,code,message,source_id=model.source_id if model else '')


def resolve_models(component, step_fetch, check=lambda: None, *, prepare_altium=False):
    """Keep each failed placement unresolved; successful 2D domains survive."""
    assets, payloads, models = {}, {}, []
    diagnostics = list(component.diagnostics)
    pads = [p for p in component.footprint.items if isinstance(p,Pad)] if component.footprint else []
    pad_bounds = (min(float(p.center.x-p.size.x/2) for p in pads),min(float(p.center.y-p.size.y/2) for p in pads),
                  max(float(p.center.x+p.size.x/2) for p in pads),max(float(p.center.y+p.size.y/2) for p in pads)) if pads else None
    for model in component.models:
        check()
        resolved = model
        if model.placement is None:
            models.append(model)
            if not any(d.domain == 'model3d' and d.severity == 'error' and d.source_id == model.source_id for d in diagnostics):
                diagnostics.append(model_diagnostic('error','missing_model_placement','模型缺少已验证的放置参数，已跳过关联',model))
            continue
        try:
            placement = model.placement
            if placement.convention != 'center-bottom-zxy-uniform':
                raise DownloadError('模型放置规则尚不支持')
            reference = ModelReference(model.name,model.identifier)
            step = step_fetch(reference)
            if not step.data:
                raise DownloadError('官方 STEP 模型不可用')
            box = step_bounds(step.data,check)
            rotation = rotation_zxy(placement.angles)
            anchor = ((box[0]+box[3])/2,(box[1]+box[4])/2,box[2])
            offset = [float(placement.position[i])-sum(rotation[i][j]*anchor[j] for j in range(3)) for i in range(3)]
            matrix = tuple(D(str(rotation[i][j] if j<3 else offset[i])) for i in range(3) for j in range(4)) + (D(0),D(0),D(0),D(1))
            if any(abs(v)>10_000_000 for v in matrix):
                raise DownloadError('模型放置超出目标坐标范围')
            payload = step.data
            if prepare_altium:
                if any(abs(v) > 5454 for v in (*offset,*transform_bounds(box,matrix))):
                    raise DownloadError('模型放置超出 AD 坐标范围，已跳过关联')
            digest = hashlib.sha256(payload).hexdigest()
            assets[digest] = ModelAsset(getattr(step,'identifier','') or model.identifier,model.name,'STEP',digest,
                                       '',tuple(D(str(v)) for v in box),digest)
            payloads[digest] = payload
            resolved = replace(model,translation=tuple(D(str(v)) for v in offset),rotation=placement.angles,
                               scale=(D(1),)*3,asset_id=digest,transform=matrix)
            if pad_bounds:
                b = transform_bounds(box,matrix)
                span = max(pad_bounds[2]-pad_bounds[0],pad_bounds[3]-pad_bounds[1],1.)
                distance = math.hypot((b[0]+b[3]-pad_bounds[0]-pad_bounds[2])/2,
                                      (b[1]+b[4]-pad_bounds[1]-pad_bounds[3])/2)
                # A modest negative stand-off is legitimate. Only flag a Z
                # displacement much larger than both footprint and model.
                height = b[5]-b[2]
                if distance > max(20.,span*5) or abs(float(placement.position[2])) > max(20.,span*5,height*5):
                    diagnostics.append(model_diagnostic('warning','model_far_from_footprint','官方模型位置明显远离封装，已保留原放置参数',model))
                model_span = max(b[3]-b[0],b[4]-b[1])
                if model_span > span*20 or model_span < span/50:
                    diagnostics.append(model_diagnostic('warning','model_size_mismatch','官方模型与封装尺寸严重不符，已保留原放置参数',model))
            if step.used_library:
                diagnostics.append(model_diagnostic('info','model_library_resolved','使用官方 3D 库备用 STEP，保留模型原始尺寸',model))
        except Cancelled:
            raise
        except Exception as exc:
            record_error(exc,'export.model_placement_failed',part=component.part,stage='model_placement')
            message = str(exc) if isinstance(exc,DownloadError) else '模型处理失败，已跳过关联'
            diagnostics.append(model_diagnostic('error','model_placement_unavailable',message,model))
        models.append(resolved)
    if not component.models and not any(d.domain == 'model3d' and d.severity == 'error' for d in diagnostics):
        diagnostics.append(model_diagnostic('info','no_model','无关联 3D 模型'))
    features = component.required_features
    if assets and 'model-placement' not in features:
        features += ('model-placement',)
    return replace(component,models=tuple(models),model_assets=tuple(assets.values()),diagnostics=tuple(diagnostics),
                   required_features=features),payloads


def resolved_models(component, payloads):
    """Validate resource integrity at the format boundary; no raw source parsing."""
    metadata = {a.sha256:a for a in component.model_assets}
    for model in component.models:
        if not model.transform:
            continue
        raw = (payloads or {}).get(model.asset_id)
        if raw is None or hashlib.sha256(raw).hexdigest() != model.asset_id or model.asset_id not in metadata:
            raise DownloadError('模型资源缺失或校验不一致，无法写入封装')
        if len(model.scale)!=3 or min(model.scale)<=0:
            raise DownloadError('模型变换缺少有效缩放比例')
        r = [[float(model.transform[i*4+j]/model.scale[j]) for j in range(3)] for i in range(3)]
        if any(abs(sum(r[k][i]*r[k][j] for k in range(3))-(1. if i==j else 0.))>1e-7 for i in range(3) for j in range(3)):
            raise DownloadError('模型矩阵包含无法表达的剪切或无效旋转')
        determinant = sum(r[0][i]*(r[1][(i+1)%3]*r[2][(i+2)%3]-r[1][(i+2)%3]*r[2][(i+1)%3]) for i in range(3))
        if abs(determinant-1)>1e-7:
            raise DownloadError('模型矩阵包含无法表达的镜像')
        yield model,metadata[model.asset_id],raw


def model_key(component):
    return tuple(sorted((m.asset_id,tuple(m.transform)) for m in component.models if m.transform))
