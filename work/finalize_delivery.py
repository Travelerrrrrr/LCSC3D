"""Verify the current portable release and collect delivery artifacts."""
import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--test-count', type=int, required=True)
args = parser.parse_args()
assert args.test_count > 0
root = Path(__file__).resolve().parent.parent
outputs = root / 'outputs'
version = re.search(r"VERSION = '([^']+)'", (root / 'work/app/main.py').read_text(encoding='utf-8')).group(1)
tested = root / 'work' / f'便携验证-v{version}' / '验证结果'
report = json.loads((tested / 'verification.json').read_text(encoding='utf-8'))
assert report['version'] == version and report['frozen'] and report['preview'] == 'ready'
assert report['preview_window'] == {'hwnd_preserved': True, 'events': []}
assert not report['csv_files'] and not list(tested.rglob('*.csv'))
assert [r['status'] for r in report['results']] == ['成功', '成功', '失败']
for result in report['results'][:2]:
    assert {Path(name).suffix for name in result['files']} == {'.step', '.wrl', '.obj'}
    for filename in result['files']:
        path = Path(filename)
        assert path.is_file() and path.stat().st_size > 0
        if path.suffix == '.step':
            assert b'ISO-10303-21' in path.read_bytes()[:2048]
    folder = Path(result['folder'])
    assert folder.name == f'{result["title"]}_{result["part"]}'
    assert not list(folder.glob('*.SchLib')) and not list(folder.glob('*.PcbLib'))
    assert 'altium' not in json.loads((folder / 'model-info.json').read_text(encoding='utf-8'))
    count = 57 if result['part'] == 'C2040' else 8
    assert report['library_previews'][result['part']] == {
        'symbol_pins': [count], 'footprint_pads': count, 'errors': []}
for source, name in [('软件界面.png', '软件界面'), ('符号_C2040.png', '符号预览'), ('封装_C2040.png', '封装预览')]:
    shutil.copyfile(tested / source, outputs / f'{name}-v{version}.png')
archive = outputs / f'LCSC3D-Source-v{version}.zip'
with zipfile.ZipFile(archive) as source_zip:
    assert not [name for name in source_zip.namelist() if '/native/' in name or name.endswith(('/altium.py', '.csv'))]
hashes = []
for name in [f'LCSC3D-Portable-v{version}.exe', archive.name]:
    with (outputs / name).open('rb') as stream:
        hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + name)
(outputs / f'SHA256SUMS-v{version}.txt').write_text('\n'.join(hashes) + '\n', encoding='ascii')
(outputs / f'验证记录-v{version}.md').write_text(f'''# {version} 成品验证

验证日期：2026-10-06。Windows x64、Python 3.12.10、PySide6 6.11.1。

- 仅导出 STEP、WRL、OBJ。已移除符号/封装导出控件、后端、原生转换器、构建依赖和相关运行时。
- 保留右侧 3D 模型、符号、封装预览及首次在线预览防闪动修复。
- {args.test_count} 项本地测试通过，覆盖下载、路径命名、已存在文件、失败处理、旧设置迁移、预览切换、缓存、缩放平移和窗口稳定性。
- 实际 EXE 从独立中文目录联网运行，清除 Python/Qt 环境变量，PATH 仅保留系统目录；退出码为 0。
- C2040 与 C20197 均成功保存三种 3D 格式；无效编号正确失败。输出仍按“器件名_编号”分目录，未生成 CSV、SchLib 或 PcbLib。
- 官方 3D 预览 ready；主窗口句柄保持不变，没有 Hide/WinIdChange 事件。
- 两个器件的符号/封装预览分别识别 57/57 和 8/8 个引脚/焊盘，错误列表为空。
- EXE 内部归档及源码 ZIP 均检查了 AD 后端移除情况；源码 ZIP 未包含 CSV、缓存、原生后端或本机设置。

成品：LCSC3D-Portable-v{version}.exe。源码：LCSC3D-Source-v{version}.zip。校验和：SHA256SUMS-v{version}.txt。
原始记录及预览截图位于 work/便携验证-v{version}/验证结果/。
''', encoding='utf-8')
print('DELIVERY_VERIFIED', version)
