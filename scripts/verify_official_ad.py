"""Reproduce the official sample audit and save all generated native libraries."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'app/tests')]
from altium import export_schlib, export_pcblib
from altium_inspect import schematic, pcb, region
from test_official_altium import OfficialAltiumTests, cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=Path, default=ROOT / 'outputs/官方AD样本-20261007')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/官方样本转换修复验证-20261007')
    args = parser.parse_args()
    samples = cases()
    for case in samples:
        source = next(args.samples.glob(case['part'] + '_*/官方器件源数据.json'))
        if hashlib.sha256(source.read_bytes()).hexdigest() != case['source_sha256']:
            raise RuntimeError('官方样本输入发生变化：' + case['part'])
        for field, ext in [('schematic', 'schdoc'), ('pcb', 'pcbdoc')]:
            ref = next(p for p in source.parent.rglob('*.' + ext) if '总符号' not in p.parts)
            if hashlib.sha256(ref.read_bytes()).hexdigest() != case[field + '_sha256']:
                raise RuntimeError('官方 AD 基准发生变化：' + str(ref))
        case['data'] = json.loads(source.read_text('utf-8-sig'))
        case['folder'] = source.parent.name
    log = io.StringIO()
    with patch('test_official_altium.cases', return_value=samples):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(OfficialAltiumTests))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'unittest.txt').write_text(log.getvalue(), 'utf-8')
    if not result.wasSuccessful():
        print(log.getvalue())
        return 1
    report = {'date': '2026-10-07', 'official_converter': 'chameleon 4.1.43',
              'source': 'JLCEDA/EasyEDA official library', 'passed': True,
              'regression_tests': result.testsRun, 'components': [],
              'limits': ['Not opened or placed in Altium Designer.',
                         'Original JSON electrical types retained; official export replaces some with Passive.',
                         'Custom pad anchors may differ; full copper contours are checked within 0.00127 mm.']}
    table = []
    for case in samples:
        folder = args.output / '原生库' / case['folder']
        folder.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for ext, export in [('SchLib', export_schlib), ('PcbLib', export_pcblib)]:
            path = folder / (case['part'] + '.' + ext)
            payload = export(case['data'], case['part'])
            path.write_bytes(payload)
            outputs[ext] = {'path': path.relative_to(args.output).as_posix(),
                            'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
        header, _, pins = schematic((folder / (case['part'] + '.SchLib')).read_bytes())
        _, pads, primitives = pcb((folder / (case['part'] + '.PcbLib')).read_bytes())
        source_polygons = sum(s.startswith('PAD~POLYGON~') for s in case['data']['packageDetail']['dataStr']['shape'])
        custom = len({int(meta['PADINDEX']) for kind, body in primitives if kind == 11
                      for _, meta, _ in [region(body)] if 'PADINDEX' in meta})
        entry = {'part': case['part'], 'title': case['data']['title'], 'units': int(header['PARTCOUNT0'])-1,
                 'pins': len(pins), 'pads_and_holes': len(pads), 'custom_pads': custom,
                 'source_polygon_pads': source_polygons, 'recognized_native_pads': source_polygons-custom,
                 'pcb_primitives': len(pads)+len(primitives), 'source_sha256': case['source_sha256'],
                 'passed': True, 'outputs': outputs}
        report['components'].append(entry)
        table.append(f"| {entry['part']} | {entry['units']} | {entry['pins']} | {entry['pads_and_holes']} | {custom} | 通过 |")
    (args.output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    text = '\n'.join([
        '# 官方样本转换修复验证', '', '日期：2026-10-07。27 个原始 JSON、官方 ASCII SchDoc/PcbDoc 均核对 SHA-256。', '',
        '27 个器件全部生成原生 SchLib/PcbLib，共 54 份；使用独立 olefile 读取器核对实际库记录。', '',
        '修复：多单元归属及各自原点；真实引脚编号；按轮廓识别标准焊盘；自定义铜区使用从 1 开始的全图元索引关联到焊盘；多层异形铜轮廓；yes/no 等镀孔值；槽孔中心及铜孔偏移；实心极性圆点。', '',
        '几何回归核对单元数量、所有引脚坐标/方向/名称/编号、矩形与椭圆主体、焊盘尺寸/位置/孔径/槽长/旋转/偏移/扩展，以及自定义铜区与焊盘关联。', '',
        '官方转换链会把部分电气类型改成被动；本项目保留原 JSON 声明。无圆角矩形使用原生 RECT，形状与官方零半径 ROUNDRECT 等价。', '',
        '标准焊盘参考容差 0.0011 mil；自定义轮廓/曲线容差 0.05 mil（0.00127 mm）。自定义铜区逐轮廓比对，内部锚点位置可与官方不同。', '',
        '尚未完成 Altium Designer 实机打开、放置和 PCB 更新验收。多轮廓区域仍明确失败；PcbLib 不内嵌 STEP。', '',
        '| 编号 | 单元 | 引脚 | 焊盘及孔 | 自定义焊盘 | 结果 |', '|---|---:|---:|---:|---:|---|', *table, '',
        '完整结果与文件校验和：verification.json。原生库：原生库/。回归日志：unittest.txt。', ''])
    (args.output / '检查报告.md').write_text(text, 'utf-8')
    print(f"27 components, 54 libraries, {result.testsRun} semantic regression checks passed: {args.output.resolve()}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
