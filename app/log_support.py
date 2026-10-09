"""Collect bounded diagnostic snapshots and clear only application log files."""
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import uuid
import zipfile

from app_logging import (diagnostic_status, locked_log_files, reset_active_log,
                         log_event, record_error, traced)
from app_paths import data_directory
from app_settings import get_preferences


LOG_NAME = re.compile(r'LCSC3D(?:-update)?\.log(?:\.[1-9][0-9]*)?|LCSC3D-crash-[0-9]+\.log')
MARKER_NAME = re.compile(r'run-[0-9]+\.json')
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_FILES = 128
FEEDBACK_TEXT = '''LCSC3D 问题反馈

请将这个 ZIP 发给开发者，并补充：
1. 出问题的大致时间。
2. 具体操作步骤、期望结果、实际结果。
3. 完整报错截图；下载或预览问题请提供元件 C 编号和导出格式。
4. 网络问题请说明是否使用代理/VPN、同机浏览器能否正常访问对应网站。

diagnostics.json 包含软件/系统版本、当前生效的代理与日志设置、收集结果。
logs/ 包含当时的主程序、更新助手、轮转及崩溃日志，还有运行状态标记。
不收集登录会话、账号密码、完整配置、代理地址、环境变量或下载的模型。
崩溃线程栈可能包含运行时文件路径。包内文件是点击打包时的快照。
如 diagnostics.json 的 issues 非空，部分文件可能缺失或截短。
保持 Debug，复现后及时打包；报错后切换等级不能补回此前的日志。
'''


def _files(folder, *, markers=False):
    """Never recurse, follow links, or collect arbitrary files in the log folder."""
    found, issues = [], []
    if not folder.exists():
        return found, issues
    for path in folder.iterdir():
        if not LOG_NAME.fullmatch(path.name) and not (markers and MARKER_NAME.fullmatch(path.name)):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                issues.append({'file': path.name, 'reason': 'not_regular_file'})
                continue
            found.append((info.st_mtime, path))
        except OSError as exc:
            issues.append({'file': path.name, 'reason': type(exc).__name__})
    return [path for _, path in sorted(found, key=lambda entry: (-entry[0], entry[1].name))], issues


def _marker(data):
    value = json.loads(data)
    pid, identity = value.get('pid'), value.get('session_id')
    if type(pid) is not int or pid <= 0 or not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise ValueError('Invalid run marker')
    return json.dumps({'pid': pid, 'session_id': identity}).encode('utf-8')


@traced('logs.package')
def package_logs():
    from PySide6.QtCore import qVersion
    created = datetime.now().astimezone()
    metadata = {'schema_version': 1, 'created_at': created.isoformat(timespec='seconds'),
                'application': diagnostic_status(), 'preferences': get_preferences().to_mapping(),
                'environment': {'system': platform.system(), 'release': platform.release(),
                                'os_version': platform.version(), 'architecture': platform.machine(),
                                'python': platform.python_version(), 'qt': qVersion(),
                                'frozen': bool(getattr(sys, 'frozen', False))},
                'files': [], 'issues': []}
    snapshots, remaining = [], MAX_BUNDLE_BYTES
    with locked_log_files() as folder:
        paths, issues = _files(folder, markers=True)
        metadata['issues'].extend(issues)
        for path in paths[:MAX_FILES]:
            try:
                with path.open('rb') as stream:
                    size = os.fstat(stream.fileno()).st_size
                    limit = min(MAX_FILE_BYTES, remaining)
                    if MARKER_NAME.fullmatch(path.name):
                        if size > 2048:
                            raise ValueError('Oversized run marker')
                        data = _marker(stream.read(2048))
                        if len(data) > remaining:
                            metadata['issues'].append({'file': path.name, 'reason': 'bundle_size_limit'})
                            continue
                    else:
                        if size and not limit:
                            metadata['issues'].append({'file': path.name, 'reason': 'bundle_size_limit'})
                            continue
                        truncated = size > limit
                        if truncated:
                            stream.seek(size - limit)
                        data = stream.read(min(size, limit))
                        if truncated:
                            # Keep complete lines, including complete JSON log entries.
                            data = data.partition(b'\n')[2]
                            metadata['issues'].append({'file': path.name, 'reason': 'truncated', 'original_bytes': size})
                    remaining -= len(data)
                    snapshots.append((path.name, data))
                    metadata['files'].append({'file': path.name, 'bytes': len(data)})
            except (OSError, ValueError, AttributeError) as exc:
                metadata['issues'].append({'file': path.name, 'reason': type(exc).__name__})
                record_error(exc, 'logs.package_file_failed', level='WARNING')
        if len(paths) > MAX_FILES:
            metadata['issues'].append({'reason': 'file_count_limit', 'omitted': len(paths) - MAX_FILES})
    destination = data_directory() / 'diagnostics'
    destination.mkdir(parents=True, exist_ok=True)
    filename = 'LCSC3D-logs-' + created.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.zip'
    target = destination / filename
    temporary = target.with_suffix('.tmp')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in snapshots:
                archive.writestr('logs/' + name, data)
            archive.writestr('diagnostics.json', json.dumps(metadata, ensure_ascii=False, indent=2))
            archive.writestr('反馈说明.txt', FEEDBACK_TEXT)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    count = sum(bool(LOG_NAME.fullmatch(name)) for name, _ in snapshots)
    log_event('INFO', 'logs.package_completed', files=count, issues=len(metadata['issues']))
    return {'path': target, 'log_count': count, 'issues': metadata['issues']}


@traced('logs.clear')
def clear_logs():
    cleared, failures = [], []
    with locked_log_files() as folder:
        paths, failures = _files(folder)
        for path in paths:
            try:
                if not reset_active_log(path):
                    # Retain dumps owned by other active sessions. Their run markers
                    # also remain intact, so clearing cannot invent an unclean exit.
                    if path.name.startswith('LCSC3D-crash-'):
                        marker = folder / ('run-' + path.stem.removeprefix('LCSC3D-crash-') + '.json')
                        if marker.exists():
                            failures.append({'file': path.name, 'reason': 'session_marker_present'})
                            continue
                    path.unlink(missing_ok=True)
                cleared.append(path.name)
            except OSError as exc:
                failures.append({'file': path.name, 'reason': type(exc).__name__})
                record_error(exc, 'logs.clear_file_failed', level='WARNING')
    log_event('WARNING' if failures else 'INFO', 'logs.cleared', cleared=len(cleared), failures=len(failures))
    return {'cleared': cleared, 'failures': failures}
