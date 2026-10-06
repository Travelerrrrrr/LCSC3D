"""Run the isolated EasyKiConverter adapter without launching a visible console."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from errors import Cancelled, DownloadError

CONVERTER_COMMIT = '1473cc9908cc623fdf301e5a1d524f71f91d184a'
CONVERTER_URL = 'https://github.com/EasyKiconverter/EasyKiConverter'
ALTIUM_FORMATS = {'SCHLIB': 'SchLib', 'PCBLIB': 'PcbLib'}
OLE_MAGIC = bytes.fromhex('d0cf11e0a1b11ae1')


def converter_path() -> Path:
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    path = root / 'native/runtime/lcsc-altium.exe'
    if not path.is_file():
        raise DownloadError('AD 转换组件尚未构建，请按 README 构建 native 后端或使用完整便携版')
    return path


@dataclass
class LibraryExport:
    payloads: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    details: dict = field(default_factory=dict)


def export_libraries(data: dict, part: str, formats: tuple[str, ...],
                     check_cancelled: Callable[[], None], step: bytes | None = None) -> LibraryExport:
    executable = converter_path()
    check_cancelled()
    with tempfile.TemporaryDirectory(prefix='lcsc-ad-') as directory:
        folder = Path(directory)
        source = folder / 'component.json'
        source.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        command = [str(executable), '--input', str(source), '--lib-name', part]
        targets = {}
        for fmt in formats:
            target = folder / f'{part}.{ALTIUM_FORMATS[fmt]}'
            targets[fmt] = target
            command.extend(['--symbol' if fmt == 'SCHLIB' else '--footprint', str(target)])
        if step:
            target = folder / 'model.step'
            target.write_bytes(step)
            command.extend(['--step', str(target)])
        env = os.environ.copy()
        # Qt and Python settings from the parent must not redirect the private runtime.
        for key in list(env):
            if key.startswith(('QT_', 'QML_', 'PYTHON')):
                env.pop(key)
        env['PATH'] = str(executable.parent) + os.pathsep + str(Path(env.get('WINDIR', r'C:\Windows')) / 'System32')
        env['QT_LOGGING_RULES'] = '*.debug=false'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        started = time.monotonic()
        process = subprocess.Popen(command, cwd=folder, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=flags)
        try:
            while True:
                check_cancelled()
                if time.monotonic() - started > 45:
                    raise DownloadError('AD 转换超时，请重试')
                try:
                    stdout, stderr = process.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        check_cancelled()
        if process.returncode:
            message = stderr.decode('utf-8', errors='replace').strip()[-600:]
            raise DownloadError(f'AD 转换失败（退出码 {process.returncode}）' + (f'：{message}' if message else ''))
        try:
            details = json.loads(stdout.decode('utf-8'))
            if not isinstance(details, dict):
                raise ValueError('invalid response')
        except (ValueError, UnicodeDecodeError) as exc:
            raise DownloadError('AD 转换组件返回了无法识别的结果') from exc
        exported = LibraryExport(details=details)
        for fmt, target in targets.items():
            info = details.get('symbol' if fmt == 'SCHLIB' else 'footprint', {})
            if not info.get('ok'):
                exported.errors[fmt] = '官方库缺少可转换的符号或封装数据' if 'Official library' in info.get('error', '') else '库文件转换失败'
                continue
            payload = target.read_bytes() if target.is_file() else b''
            if len(payload) < 512 or not payload.startswith(OLE_MAGIC):
                exported.errors[fmt] = '转换结果不是有效的 AD 库文件'
            else:
                exported.payloads[fmt] = payload
        return exported
