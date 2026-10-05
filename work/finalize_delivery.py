import hashlib
import json
import shutil
from pathlib import Path

root = Path(__file__).parent.parent
outputs = root / 'outputs'
tested = root / 'work' / '便携验证' / '验证结果'
report = json.loads((tested / 'verification.json').read_text(encoding='utf-8'))
assert report['frozen'] and report['preview'] == 'ready'
assert [r['status'] for r in report['results']] == ['成功', '成功', '失败']
for result in report['results'][:2]:
    assert len(result['files']) == 3
    for filename in result['files']:
        path = Path(filename)
        assert path.is_file() and path.stat().st_size > 0
        if path.suffix == '.step':
            assert b'ISO-10303-21' in path.read_bytes()[:2048]
shutil.copyfile(tested / '软件界面.png', outputs / '软件界面.png')
hashes = []
for name in ['LCSC3D-Portable.exe', 'LCSC3D-Source.zip']:
    with (outputs / name).open('rb') as stream:
        hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + name)
(outputs / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='ascii')
(outputs / '验证记录.md').write_text('''# 成品验证记录

验证日期：2026-10-06。

- 成品：LCSC3D-Portable.exe，Windows x64，单文件便携版。
- 从独立的中文目录运行复制的 EXE，工作目录与源码目录分离。
- 清空 PYTHONPATH、PYTHONHOME、Qt 插件与资源路径；PATH 仅保留 Windows 系统目录。
- EXE 自带 Python、Qt 和浏览器运行时，无须另装 Python、KiCad、WebView2。
- 混合换行和逗号输入 c20197、C2040 等编号；不区分大小写，重复 C2040 自动合并。
- C2040 与 C20197 均成功下载 STEP、WRL、OBJ，文件非空，STEP 文件包含 ISO-10303-21 标记。
- 不存在的 C999999999999 显示“未找到器件，请核对立创 C 编号”，批次继续完成并生成 CSV 报告。
- C2040 的官方在线查看器完成模型加载，实际 EXE 的预览状态为 ready。
- 8 个本地检查通过，覆盖输入去重、网络失败、已有文件保留、零字节文件恢复、格式部分失败、取消请求与输出路径处理。

下载和预览仍依赖官方服务器及网络；预览依赖 WebGL。测试未覆盖每一种器件或所有 Windows/显卡配置。
源码、第三方说明和构建脚本见 LCSC3D-Source.zip。文件 SHA-256 见 SHA256SUMS.txt。
''', encoding='utf-8')
print('DELIVERY_VERIFIED')
for path in sorted(outputs.iterdir()):
    print(path.name, path.stat().st_size)
