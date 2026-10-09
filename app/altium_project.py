"""Register library documents in an existing Altium PCB project."""
from __future__ import annotations

import os
from pathlib import Path
import re
import uuid

from app_paths import data_directory
from app_logging import traced, log_event
from errors import DownloadError


def commit_user_file(path, original, payload, check_cancelled=lambda: None):
    """Back up user data in AppData and refuse stale read/modify/write results."""
    from backend import atomic_write
    path = Path(path)
    check_cancelled()
    if path.read_bytes() != original:
        raise DownloadError('文件在处理期间已被修改，请重新执行；保留当前文件')
    if payload == original:
        return None
    backup = data_directory() / 'backups' / (uuid.uuid4().hex + '-' + path.stem[:80] + path.suffix)
    atomic_write(backup, original)
    check_cancelled()
    if path.read_bytes() != original:
        raise DownloadError('文件在备份期间已被修改，请重新执行；保留当前文件')
    atomic_write(path, payload)
    log_event('INFO', 'library.user_file_saved', format=path.suffix.lower(), backed_up=True)
    return backup


def read_project(path):
    path = Path(path)
    if path.suffix.lower() != '.prjpcb' or path.stat().st_size > 4 * 1024 * 1024:
        raise DownloadError('请选择有效的 .PrjPcb 工程文件')
    original = path.read_bytes()
    if original.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    elif original.startswith(b'\xef\xbb\xbf'):
        encoding = 'utf-8-sig'
    else:
        encoding = 'utf-8'
        try:
            original.decode(encoding)
        except UnicodeDecodeError:
            encoding = 'gb18030'
            try:
                original.decode(encoding)
            except UnicodeDecodeError:
                encoding = 'cp1252'
    text = original.decode(encoding)
    if not re.search(r'^\[Design\]\s*$', text, re.M | re.I) or '\x00' in text:
        raise DownloadError('工程格式无法识别，未修改原文件')
    return original, text, encoding


@traced('library.project_import', lambda project, libraries, *a: {'count': len(libraries)}, level='INFO')
def add_libraries_to_project(project, libraries, check_cancelled=lambda: None):
    project = Path(project).resolve()
    original, text, encoding = read_project(project)
    entries = list(re.finditer(r'^\[([^]\r\n]+)\][ \t]*\r?$', text, re.M))
    known, indices = set(), []
    for i, match in enumerate(entries):
        if re.fullmatch(r'Document[0-9]+', match[1], re.I):
            indices.append(int(re.search(r'[0-9]+$', match[1])[0]))
            block = text[match.end():entries[i+1].start() if i+1 < len(entries) else len(text)]
            path = re.search(r'^DocumentPath=(.*)\r?$', block, re.M | re.I)
            if path:
                known.add(os.path.normcase(str((project.parent / path[1].strip()).resolve())))
    added, skipped = [], 0
    newline = '\r\n' if '\r\n' in text else '\n'
    suffix = ''
    index = max(indices, default=0)
    for name in dict.fromkeys(str(Path(name).resolve()) for name in libraries):
        check_cancelled()
        library = Path(name)
        if library.suffix.lower() not in ('.schlib', '.pcblib') or not library.is_file():
            raise DownloadError('仅能导入已存在的 .SchLib / .PcbLib 文件')
        canonical = os.path.normcase(name)
        if canonical in known:
            skipped += 1
            continue
        known.add(canonical)
        try:
            relative = os.path.relpath(library, project.parent)
        except ValueError:
            relative = name
        if any(char in relative for char in '\r\n\x00'):
            raise DownloadError('库路径包含工程不支持的字符')
        index += 1
        suffix += newline.join(('', f'[Document{index}]', f'DocumentPath={relative}',
                                'AnnotationEnabled=0', 'DoLibraryUpdate=1', 'DoDatabaseUpdate=1',
                                'DocumentUniqueId=' + uuid.uuid4().hex[:8].upper(), ''))
        added.append(name)
    backup = None
    if added:
        if text and not text.endswith(('\r', '\n')):
            suffix = newline + suffix
        # Encode the addition alone so every existing byte remains unchanged.
        codec = 'utf-16le' if original.startswith(b'\xff\xfe') else 'utf-16be' if original.startswith(b'\xfe\xff') else encoding.removesuffix('-sig')
        try:
            payload = original + suffix.encode(codec)
        except UnicodeEncodeError as exc:
            raise DownloadError('工程编码无法表示库路径，请使用可兼容的目录名称') from exc
        check_cancelled()
        backup = commit_user_file(project, original, payload, check_cancelled)
    return {'added': len(added), 'skipped': skipped, 'backup': str(backup) if backup else ''}
