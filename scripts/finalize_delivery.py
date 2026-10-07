"""Verify the current portable release and collect delivery artifacts."""
import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--test-count', type=int, required=True)
args = parser.parse_args()
assert args.test_count > 0
root = Path(__file__).resolve().parent.parent
outputs = root / 'outputs'
version = re.search(r"VERSION = '([^']+)'", (root / 'app/main.py').read_text(encoding='utf-8')).group(1)
tested = root / 'work' / '便携验证' / '验证结果'
report = json.loads((tested / 'verification.json').read_text(encoding='utf-8'))
update = json.loads((root / 'work/self-update-verification.json').read_text(encoding='utf-8'))
assert report['version'] == update['version'] == version
assert report['frozen'] and report['preview'] == 'ready'
assert report['application_name'] == report['window_title'] == 'LCSC3D'
assert report['automatic_preview'] and report['viewer_page_loads'] == 1 and report['second_3d_ready']
assert report['preview_window'] == {'hwnd_preserved': True, 'events': []}
assert not report['csv_files'] and not list(tested.rglob('*.csv'))
assert not report['non_model_exports']
assert [r['status'] for r in report['results']] == ['成功', '成功', '失败']
assert report['download_selection'] == {
    'checked_ids': ['C2040', 'C20197', 'C999999999999'],
    'unchecked_ids': ['C163691'], 'selected_only': True}
assert report['titles_before_download']['C2040'] == 'RP2040'
assert report['titles_before_download']['C20197'] == '4D03WGJ0102T5E'
for result in report['results'][:2]:
    assert len(result['files']) == 2
    assert {Path(name).suffix for name in result['files']} == {'.step', '.obj'}
    for filename in result['files']:
        path = Path(filename)
        assert path.is_file() and path.stat().st_size > 0
        if path.suffix == '.step':
            assert b'ISO-10303-21' in path.read_bytes()[:2048]
    folder = Path(result['folder'])
    assert folder.name == f'{result["title"]}_{result["part"]}'
    assert set(folder.iterdir()) == {Path(file) for file in result['files']}
    count = 57 if result['part'] == 'C2040' else 8
    preview = report['library_previews'][result['part']]
    assert preview['symbol_pins'] == [count] and preview['footprint_pads'] == count and not preview['errors']
for key in ('frozen_helper', 'original_exited', 'replacement_verified', 'startup_acknowledged', 'settings_preserved'):
    assert update[key]
for source, name in [('软件界面.png', '软件界面'), ('符号_C2040.png', '符号预览'),
                     ('封装_C2040.png', '封装预览'), ('型号查询.png', '型号查询')]:
    shutil.copyfile(tested / source, outputs / f'{name}.png')
shutil.copyfile(tested / '符号_C2040.png', root / 'docs/images/app.png')
shutil.copyfile(tested / '封装_C2040.png', root / 'docs/images/footprint.png')
archive = outputs / 'LCSC3D.zip'
with zipfile.ZipFile(archive) as source_zip:
    names = source_zip.namelist()
    assert not any('/upstream/' in name or 'easyeda2kicad' in name or name.endswith('.csv') for name in names)
    assert all('LCSC3D/' + name in names for name in ('app/updater.py', 'app/model3d.py', 'app/resources.py',
                                                     'app/vector_viewer.html', 'LICENSE'))
pyz = CArchiveReader(str(outputs / 'LCSC3D.exe')).open_embedded_archive('PYZ.pyz')
assert not any('easyeda2kicad' in name for name in pyz.toc)
assert all(name in pyz.toc for name in ('updater', 'update_ui', 'model3d', 'resources'))
hashes = []
for name in ('LCSC3D.exe', archive.name):
    with (outputs / name).open('rb') as stream:
        hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + name)
(outputs / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='ascii')
text = f"""# LCSC3D {version} 成品验证

验证日期：2026-10-07。Windows x64、Python 3.12.10、PySide6 6.11.1。

- {args.test_count} 项本地回归测试通过：官方 STEP/OBJ 下载、下载目录仅有模型文件、拒绝已取消的导出格式、旧设置迁移、多单元符号预览、型号查询、勾选过滤、并发、取消、缓存、镜像回退、预览交互及窗口稳定性。
- 更新测试覆盖正式版本比较、发行附件、HTTPS 地址、SHA-256、大小校验、取消、文件变化、替换重启、启动失败恢复、源码运行与 UI 重试。
- 独立中文目录运行真实 EXE，清除 Python/Qt 环境变量并限制 PATH，退出码 0。
- C2040 与 C20197 各保存 2 个模型文件：官方 STEP/OBJ；无效编号失败，未勾选 C163691 不下载。
- 已取消 JSON 和 SVG 导出，包括 model-info.json；下载目录检查没有任何非模型文件，符号和封装仍可预览。软件界面与说明继续标注 JLCEDA/EasyEDA 官方库来源。
- 两个器件的本地 3D 预览均 ready，复用一个查看器页面。符号/封装分别识别 57/57 和 8/8 个引脚/焊盘，错误列表为空。
- 主窗口句柄保持不变，没有 Hide/WinIdChange 事件。
- 实际冻结 EXE 自更新验证通过：等待原程序退出、独立更新进程替换、重启 Qt 窗口并确认、保留设置，耗时 {update['seconds']} 秒。候选为本版的相同副本；版本发现和失败恢复由本地测试覆盖。
- EXE 内部 Python 归档与源码 ZIP 已检查，不含 easyeda2kicad 或上游子模块。

成品：LCSC3D.exe。源码：LCSC3D.zip。校验和：SHA256SUMS.txt。
原始记录：work/便携验证/验证结果/verification.json、work/self-update-verification.json。
"""
(outputs / '验证记录.md').write_text(text, encoding='utf-8')
(root / 'docs/验证记录.md').write_text(text, encoding='utf-8')
print('DELIVERY_VERIFIED', version)
