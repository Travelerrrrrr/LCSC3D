"""Versioned library/project backups, explicit restoration and bounded cleanup."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from app_paths import data_directory
from app_logging import log_event, record_error
from errors import DownloadError
from i18n import text as ui_text

SUFFIXES = {'.schlib', '.pcblib', '.prjpcb'}
MAX_BYTES = 512 * 1024 * 1024
LOCK = threading.RLock()


def backup_directory():
    return data_directory() / 'backups'


def linked(path):
    return path.is_symlink() or path.is_junction()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def read_payload(path):
    if linked(path) or path.stat().st_size > MAX_BYTES:
        raise DownloadError(ui_text('备份文件不可用或超过 512 MiB，未恢复。'))
    return path.read_bytes()


@dataclass(frozen=True)
class Backup:
    id: str
    path: Path
    name: str
    created: str
    size: int
    stored_size: int
    original_path: str = ''
    sha256: str = ''
    legacy: bool = False
    error: bool = False


def list_backups():
    root = backup_directory()
    if not root.exists():
        return []
    if linked(root):
        raise DownloadError(ui_text('备份目录不能是链接，请检查应用数据目录。'))
    result = []
    for path in root.iterdir():
        if linked(path):
            continue
        try:
            if path.is_file() and re.fullmatch(r'[0-9a-f]{32}-.+', path.name) and path.suffix.lower() in SUFFIXES:
                stat = path.stat()
                result.append(Backup(path.name, path, path.name[33:],
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), stat.st_size, stat.st_size, legacy=True))
            elif path.is_dir() and re.fullmatch(r'[0-9a-f]{32}', path.name):
                files = list(path.iterdir())
                if any(linked(file) for file in files):
                    continue
                payloads = [file for file in files if file.is_file() and file.stem == 'original' and file.suffix.lower() in SUFFIXES]
                metadata = path / 'metadata.json'
                if len(payloads) != 1:
                    continue
                payload = payloads[0]
                stat = payload.stat()
                size = sum(file.stat().st_size for file in files if file.is_file())
                row = Backup(path.name, payload, payload.name,
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), stat.st_size, size, error=True)
                try:
                    if metadata.stat().st_size > 65536:
                        raise ValueError('Metadata too large')
                    value = json.loads(metadata.read_text('utf-8'))
                    target = value['original_path']
                    created = value['created']
                    datetime.fromisoformat(created)
                    if (value['version'] != 1 or not isinstance(target, str) or not Path(target).is_absolute()
                            or Path(target).suffix.lower() != payload.suffix.lower()
                            or type(value['size']) is not int or value['size'] != stat.st_size
                            or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])):
                        raise ValueError('Invalid backup metadata')
                    row = Backup(path.name, payload, Path(target).name, created, stat.st_size, size, target, value['sha256'])
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                result.append(row)
        except FileNotFoundError:
            continue  # Another application instance removed this entry.
    return sorted(result, key=lambda row: (row.created, row.id), reverse=True)


def create_backup(path, payload, *, reason='automatic'):
    from backend import atomic_write
    path = Path(path).resolve()
    if path.suffix.lower() not in SUFFIXES or len(payload) > MAX_BYTES:
        raise DownloadError(ui_text('仅支持备份不超过 512 MiB 的 SchLib、PcbLib 和 PrjPcb 文件。'))
    root = backup_directory()
    if linked(root):
        raise DownloadError(ui_text('备份目录不能是链接，请检查应用数据目录。'))
    with LOCK:
        root.mkdir(parents=True, exist_ok=True)
        key = uuid.uuid4().hex
        stage, destination = root / ('.pending-' + key), root / key
        stage.mkdir()
        data = stage / ('original' + path.suffix)
        metadata = stage / 'metadata.json'
        try:
            atomic_write(data, payload)
            value = dict(version=1, original_path=str(path), created=datetime.now(timezone.utc).isoformat(),
                         size=len(payload), sha256=digest(payload), reason=reason)
            atomic_write(metadata, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))
            stage.rename(destination)
        finally:
            if stage.exists():
                data.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                stage.rmdir()
        log_event('INFO', 'backup.created', format=path.suffix.lower(), bytes=len(payload), reason=reason)
        return destination / data.name


def get_backup(key):
    row = next((row for row in list_backups() if row.id == key), None)
    if row is None or row.error:
        raise DownloadError(ui_text('备份已删除或信息损坏，请刷新列表。'))
    return row


def verified_payload(row):
    payload = read_payload(row.path)
    if not row.legacy:
        if len(payload) != row.size or digest(payload) != row.sha256:
            raise DownloadError(ui_text('备份完整性校验失败，未修改目标文件。'))
    elif row.path.suffix.lower() == '.prjpcb':
        from altium_project import read_project
        read_project(row.path)
    else:
        from library_merge import LibraryIndex
        index = LibraryIndex(payload, row.path.suffix[1:].upper())
        index.ole.close()
    return payload


@dataclass(frozen=True)
class RestorePlan:
    backup_id: str
    backup_sha256: str
    target: Path
    target_sha256: str | None


def target_payload(target):
    return read_payload(target) if target.exists() else None


def prepare_restore(key, target):
    """Capture the exact target state shown in the confirmation, without writing."""
    with LOCK:
        row = get_backup(key)
        target = Path(target).expanduser()
        if not target.is_absolute() or target.suffix.lower() != row.path.suffix.lower():
            raise DownloadError(ui_text('请输入绝对路径，且目标文件类型必须与备份一致。'))
        if linked(target):
            raise DownloadError(ui_text('恢复目标不能是文件链接。'))
        target = target.resolve()
        if target.is_relative_to(backup_directory().resolve()):
            raise DownloadError(ui_text('不能将备份目录作为恢复目标。'))
        original = target_payload(target)
        return RestorePlan(key, digest(verified_payload(row)), target, digest(original) if original is not None else None)


def restore_backup(plan):
    from backend import atomic_write
    with LOCK:
        row = get_backup(plan.backup_id)
        payload = verified_payload(row)
        if digest(payload) != plan.backup_sha256:
            raise DownloadError(ui_text('备份内容已变化，请重新选择并确认。'))
        original = target_payload(plan.target)
        if (digest(original) if original is not None else None) != plan.target_sha256:
            raise DownloadError(ui_text('目标文件在确认后已变化，请重新确认；未覆盖当前文件。'))
        if original == payload:
            return dict(path=str(plan.target), changed=False, safety_backup='')
        safety = create_backup(plan.target, original, reason='before_restore') if original is not None else None
        current = target_payload(plan.target)
        if current != original:
            raise DownloadError(ui_text('目标文件在确认后已变化，请重新确认；未覆盖当前文件。'))
        atomic_write(plan.target, payload)
        log_event('INFO', 'backup.restored', format=plan.target.suffix.lower(), bytes=len(payload), backed_up=safety is not None)
        return dict(path=str(plan.target), changed=True, safety_backup=str(safety) if safety else '')


def clear_backups(keys):
    """Remove only the entries shown in the confirmation; keep later backups."""
    cleared, failed = [], []
    with LOCK:
        rows = {row.id: row for row in list_backups()}
        for key in dict.fromkeys(keys):
            row = rows.get(key)
            if row is None:
                continue
            try:
                if not row.legacy:
                    folder = row.path.parent
                    allowed = {row.path.name, 'metadata.json'}
                    if any(file.name not in allowed or linked(file) for file in folder.iterdir()):
                        raise OSError('Unexpected backup directory contents')
                if not row.legacy:
                    (row.path.parent / 'metadata.json').unlink(missing_ok=True)
                row.path.unlink()
                if not row.legacy:
                    row.path.parent.rmdir()
                cleared.append(key)
            except OSError as exc:
                record_error(exc, 'backup.clear_failed', level='WARNING')
                failed.append(key)
        log_event('INFO', 'backup.cleared', count=len(cleared), failures=len(failed))
    return dict(cleared=cleared, failed=failed)
