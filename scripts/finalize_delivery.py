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
version = re.search(r"VERSION = '([^']+)'", (root / 'app/main.py').read_text(encoding='utf-8')).group(1)
tested = root / 'work' / '便携验证' / '验证结果'
report = json.loads((tested / 'verification.json').read_text(encoding='utf-8'))
assert report['version'] == version and report['frozen'] and report['preview'] == 'ready'
assert report['application_name'] == report['window_title'] == 'LCSC3D'
assert report['automatic_preview']
assert report['viewer_page_loads'] == 1 and report['second_3d_ready']
assert report['preview_window'] == {'hwnd_preserved': True, 'events': []}
assert not report['csv_files'] and not list(tested.rglob('*.csv'))
assert [r['status'] for r in report['results']] == ['成功', '成功', '失败']
assert report['download_selection'] == {
    'checked_ids': ['C2040', 'C20197', 'C999999999999'],
    'unchecked_ids': ['C163691'], 'selected_only': True}
titles = report['titles_before_download']
assert titles['C2040'] == 'RP2040' and titles['C20197'] == '4D03WGJ0102T5E'
assert titles['C163691'] not in ('', '—', '查询中…', '查询失败', '未提供型号')
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
    preview = report['library_previews'][result['part']]
    assert preview['source_url'] in [f'https://{host}/api/products/{result["part"]}/svgs'
                                    for host in ('lceda.cn', 'easyeda.com')]
    assert {key: value for key, value in preview.items() if key != 'source_url'} == {
        'symbol_pins': [count], 'footprint_pads': count, 'errors': []}
for source, name in [('软件界面.png', '软件界面'), ('符号_C2040.png', '符号预览'), ('封装_C2040.png', '封装预览'),
                     ('型号查询.png', '型号查询')]:
    shutil.copyfile(tested / source, outputs / f'{name}.png')
archive = outputs / 'LCSC3D.zip'
with zipfile.ZipFile(archive) as source_zip:
    assert not [name for name in source_zip.namelist() if '/native/' in name or name.endswith(('/altium.py', '.csv'))]
    assert 'LCSC3D/app/vector_viewer.html' in source_zip.namelist()
    assert 'LCSC3D/LICENSE' in source_zip.namelist()
    assert 'LCSC3D/scripts/package_source.py' in source_zip.namelist()
hashes = []
for name in ['LCSC3D.exe', archive.name]:
    with (outputs / name).open('rb') as stream:
        hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + name)
(outputs / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='ascii')
(outputs / '验证记录.md').write_text(f'''# LCSC3D 成品验证

验证日期：2026-10-06。Windows x64、Python 3.12.10、PySide6 6.11.1。

- 正式名称、应用名称和主窗口标题均为 LCSC3D，名称无 Portable、Source 或版本后缀；内部版本为 {version}。
- 已移除“在线预览”按钮；载入列表自动预览首个器件，单击其他器件自动切换，下载期间右侧预览保持可操作。
- 下载列表新增“下载”勾选列，支持全选、反选及勾选数量显示；只下载勾选器件，重新载入按编号保留选择，下载期间锁定选择但仍可预览。
- 载入列表即后台查询型号及关联模型名，未勾选器件同样显示型号；下载前的 C2040、C20197、C163691 型号记录在 titles_before_download，截图为型号查询.png。
- 本次成品验证勾选 C2040、C20197 及无效编号，取消勾选 C163691；只有勾选器件进入下载任务，未勾选器件没有下载结果或输出目录。
- 3D 模型两次预览均 ready，器件切换复用一个查看器页面；首次加载与切换耗时记录在 verification.json 的 preview_timings。
- 符号与封装画布按需启动、共用浏览器配置；单文件 EXE 不包含未用 QML、调试资源、开发工具和多余语言包。
- 模型下载使用国内官方接口、镜像回退、复用连接和最多 3 个器件并发；STEP 与 OBJ 独立请求，WRL 复用 OBJ。有界内存缓存为 16 MiB，覆盖下载跳过缓存。

- 符号、封装直接加载国内商城使用的官方 SVG，使用内置浏览器呈现官方 CSS、文字和图层配色；实际来源接口记录在 verification.json 中。
- 国内接口请求失败时自动使用 EasyEDA 官方 SVG 镜像，不依赖商城登录。
- 仅导出 STEP、WRL、OBJ，保留首次在线预览防闪动修复。
- {args.test_count} 项本地测试通过，覆盖下载前型号查询、无 3D 关联时保留型号、旧查询响应忽略、查询失败重试、查询取消及关闭、下载失败保留已知型号、勾选、全选、反选、空选择、勾选状态保留、下载中选择锁定、不连续行结果映射、多批次进度，以及下载、路径命名、失败处理、旧设置迁移、官方 SVG 保留、镜像回退、取消请求、多单元、缓存边界、连接复用、gzip、格式并发、乱序结果、查看器复用、过期响应、按需画布、缩放平移、超 2 MB SVG 和窗口稳定性。
- 实际 EXE 从独立中文目录联网运行，清除 Python/Qt 环境变量，PATH 仅保留系统目录；退出码为 0。
- C2040 与 C20197 均成功保存三种 3D 格式；无效编号正确失败。输出仍按“器件名_编号”分目录，未生成 CSV、SchLib 或 PcbLib。
- 官方 3D 预览 ready；主窗口句柄保持不变，没有 Hide/WinIdChange 事件。
- 两个器件的符号/封装预览分别识别 57/57 和 8/8 个引脚/焊盘，错误列表为空。
- EXE 内部归档及源码 ZIP 均检查了 AD 后端移除情况；源码 ZIP 未包含 CSV、缓存、原生后端或本机设置。

成品：LCSC3D.exe。源码：LCSC3D.zip。校验和：SHA256SUMS.txt。
原始记录及预览截图位于 work/便携验证/验证结果/。
性能前后对比及测试范围见 docs/性能优化.md。
''', encoding='utf-8')
shutil.copyfile(outputs / '验证记录.md', root / 'docs' / '验证记录.md')
print('DELIVERY_VERIFIED', version)
