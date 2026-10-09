"""Verified GitHub Release updates and a separate Windows replacement process."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import urllib3
from urllib3.util import Retry, Timeout

from backend import connection_pool
from errors import Cancelled
from app_settings import proxy_for_url, proxy_mode, initial_log_level
from app_logging import (log_event, record_error, traced, current_context, log_context,
                         configure_logging, network_target)
from app_paths import data_directory, updates_directory, replace_file

REPOSITORY = 'Travelerrrrrr/LCSC3D'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases/latest'
LATEST_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
STAGE_PREFIX = '.LCSC3D-update-'
MAX_EXE_SIZE = 1024 * 1024 * 1024
DOWNLOAD_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                  'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class LocalUpdateSource:
    """Explicit, process-only test source. Never loaded from saved settings."""
    origin: str
    current_version: str | None = None

    def __post_init__(self):
        try:
            if not isinstance(self.origin, str):
                raise ValueError()
            parsed = urlsplit(self.origin)
            port = parsed.port
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or parsed.username is not None or parsed.password is not None
                    or not port or parsed.netloc != f'127.0.0.1:{port}'
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise UpdateError('本地测试源必须为 http://127.0.0.1:端口') from exc
        object.__setattr__(self, 'origin', f'http://127.0.0.1:{port}')
        if self.current_version is not None:
            version_tuple(self.current_version)

    def permits(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme == 'http' and parsed.netloc == urlsplit(self.origin).netloc
                and not parsed.query and not parsed.fragment)

    def arguments(self, *, restarted=False):
        arguments = ['--local-update-source', self.origin]
        if self.current_version is not None and not restarted:
            arguments += ['--local-update-current-version', self.current_version]
        return arguments


def version_tuple(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', version):
        raise UpdateError('发行版本号无效，需要正式版本 vX.Y.Z')
    return tuple(int(value) for value in version.lstrip('v').split('.'))


@dataclass(frozen=True)
class Release:
    version: str
    page_url: str
    exe_url: str
    checksum_url: str
    size: int
    notes: str = ''
    digest: str = ''


def parse_release(data, current_version: str, *, source: LocalUpdateSource | None = None) -> Release | None:
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        raise UpdateError('更新接口没有返回正式发行版本')
    tag = data.get('tag_name')
    version = version_tuple(tag)
    demo = isinstance(current_version, str) and re.fullmatch(r'v?\d+\.\d+\.\d+-demo\.\d+', current_version)
    current = version_tuple(current_version.split('-')[0] if demo else current_version)
    if version < current or (version == current and not demo):
        return None
    base = source.origin if source is not None else f'https://github.com/{REPOSITORY}'
    prefix = f'{base}/releases/download/{quote(tag, safe="")}/'
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise UpdateError('新版本没有可用的发行附件')
    selected = {}
    for name in ('LCSC3D.exe', 'SHA256SUMS.txt'):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == name]
        if len(matches) != 1 or matches[0].get('browser_download_url') != prefix + name:
            raise UpdateError(f'发行附件 {name} 缺失或地址不受支持，请打开发布页面')
        selected[name] = matches[0]
    exe = selected['LCSC3D.exe']
    size = exe.get('size')
    if type(size) is not int or not 0 < size <= MAX_EXE_SIZE:
        raise UpdateError('更新文件大小无效')
    digest = exe.get('digest') or ''
    if digest and not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise UpdateError('更新文件的 SHA-256 元数据无效')
    return Release(tag.lstrip('v'), f'{base}/releases/tag/{quote(tag, safe="")}',
                   exe['browser_download_url'], selected['SHA256SUMS.txt']['browser_download_url'],
                   size, str(data.get('body') or '')[:24000], digest.removeprefix('sha256:').lower())


def checksum_for_exe(raw: bytes) -> str:
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise UpdateError('SHA256SUMS.txt 编码无效') from exc
    hashes = []
    for line in text.splitlines():
        match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?LCSC3D\.exe', line.strip())
        if match:
            hashes.append(match[1].lower())
    if len(hashes) != 1:
        raise UpdateError('SHA256SUMS.txt 中缺少唯一的 LCSC3D.exe 校验值')
    return hashes[0]


class UpdateClient:
    def __init__(self, cancelled: threading.Event | None = None, *, source: LocalUpdateSource | None = None):
        self.cancelled = cancelled or threading.Event()
        self.source = source

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    @traced('update.http', lambda self, url: {'host': network_target(url)})
    def _open(self, url):
        for _ in range(6):
            self.check_cancelled()
            parsed = urlsplit(url)
            allowed = (self.source.permits(url) if self.source is not None else
                       parsed.scheme == 'https' and parsed.hostname in DOWNLOAD_HOSTS and not parsed.username)
            if not allowed:
                raise UpdateError('更新服务器返回了不受支持的下载地址')
            proxy = None if self.source is not None else proxy_for_url(url, 'update')
            log_event('DEBUG', 'update.request_started',
                      source_kind='local_test' if self.source is not None else 'github',
                      proxy_mode='direct' if self.source is not None else proxy_mode('update'), using_proxy=proxy is not None)
            response = connection_pool(proxy).request('GET', url, preload_content=False, redirect=False,
                headers={'User-Agent': 'LCSC3D-updater/2.1.3', 'Accept': 'application/vnd.github+json',
                         'Accept-Encoding': 'identity'}, timeout=Timeout(connect=6, read=18), pool_timeout=18,
                retries=Retry(total=1, connect=1, read=0, status=0, redirect=0))
            log_event('DEBUG', 'update.response_received', status=response.status)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                response.close()
                response.release_conn()
                if not location:
                    raise UpdateError('更新服务器的重定向地址为空')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                status = response.status
                response.close()
                response.release_conn()
                if status in (403, 429):
                    raise UpdateError('更新服务器请求受限，请稍后重试或打开发布页面')
                raise UpdateError(f'更新服务器返回 HTTP {status}，请稍后重试')
            return response
        raise UpdateError('更新下载重定向次数过多')

    @traced('update.read', lambda self, url, *a, **kw: {'host': network_target(url)})
    def _read(self, url, maximum=1024*1024):
        response = None
        try:
            response = self._open(url)
            chunks, size = [], 0
            while True:
                self.check_cancelled()
                block = response.read(64*1024, decode_content=True)
                if not block:
                    break
                size += len(block)
                if size > maximum:
                    raise UpdateError('更新接口响应过大')
                chunks.append(block)
            self.check_cancelled()
            return b''.join(chunks)
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            self.check_cancelled()
            raise UpdateError(f'更新请求失败：{exc}') from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    @traced('update.check', lambda self, current_version: {'current_version': current_version}, level='INFO')
    def check(self, current_version):
        try:
            data = json.loads(self._read(self.source.origin + '/latest.json' if self.source is not None else LATEST_API))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpdateError('更新接口返回了无法识别的数据') from exc
        comparison = self.source.current_version if self.source is not None and self.source.current_version is not None else current_version
        result = parse_release(data, comparison, source=self.source)
        log_event('INFO', 'update.source_selected', source_kind='local_test' if self.source is not None else 'github',
                  actual_version=current_version, compared_version=comparison)
        log_event('INFO', 'update.check_result', available=result is not None, target_version=result.version if result else None)
        return result

    @traced('update.download', lambda self, release, *a, **kw: {'target_version': release.version}, level='INFO')
    def download(self, release: Release, executable: Path, progress=lambda done, total: None) -> Path:
        """Stage in local app data. Never write to the running executable."""
        target = executable.resolve(strict=True)
        self.check_cancelled()
        stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=updates_directory()))
        log_event('INFO', 'update.stage_created', update_id=stage.name, expected_bytes=release.size)
        response = None
        prepared = False
        try:
            expected = checksum_for_exe(self._read(release.checksum_url))
            if release.digest and expected != release.digest:
                log_event('ERROR', 'update.checksum_metadata_mismatch')
                raise UpdateError('发行校验文件与 GitHub 附件 SHA-256 不一致')
            response = self._open(release.exe_url)
            digest, count = hashlib.sha256(), 0
            new = stage / 'new.exe'
            progress(0, release.size)
            with new.open('xb') as stream:
                while True:
                    self.check_cancelled()
                    block = response.read(256*1024, decode_content=True)
                    if not block:
                        break
                    count += len(block)
                    if count > release.size:
                        log_event('ERROR', 'update.size_exceeded', received_bytes=count, expected_bytes=release.size)
                        raise UpdateError('下载文件超出了发行记录中的大小')
                    stream.write(block)
                    digest.update(block)
                    progress(count, release.size)
                stream.flush()
                os.fsync(stream.fileno())
            self.check_cancelled()
            if count != release.size or digest.hexdigest() != expected:
                log_event('ERROR', 'update.integrity_failed', received_bytes=count, expected_bytes=release.size,
                          size_matches=count == release.size, digest_matches=digest.hexdigest() == expected)
                raise UpdateError('更新文件不完整或 SHA-256 校验失败，原程序已保留')
            with new.open('rb') as stream:
                if stream.read(2) != b'MZ':
                    raise UpdateError('更新附件不是 Windows 可执行文件')
            plan = {'target': str(target), 'sha256': expected, 'original_sha256': file_hash(target),
                    'target_directory': str(target.parent),
                    'diagnostics': {key: value for key, value in current_context().items()
                                    if key in ('operation_id', 'parent_id', 'root_id')},
                    'parent_pid': os.getpid(), 'nonce': secrets.token_hex(24), 'version': release.version}
            if self.source is not None:
                plan['local_test'] = {'origin': self.source.origin, 'current_version': self.source.current_version}
            manifest = stage / 'plan.json'
            manifest.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
            prepared = True
            log_event('INFO', 'update.verified', update_id=stage.name, bytes=count)
            return manifest
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            self.check_cancelled()
            raise UpdateError(f'更新文件下载失败：{exc}') from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()
            if not prepared:
                shutil.rmtree(stage)


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _load_plan(manifest):
    manifest = Path(manifest).resolve(strict=True)
    stage = manifest.parent
    if manifest.name != 'plan.json' or not stage.name.startswith(STAGE_PREFIX) or stage.is_symlink():
        raise UpdateError('更新计划路径无效')
    plan = json.loads(manifest.read_text(encoding='utf-8'))
    target = Path(plan['target'])
    if not target.is_absolute() or target.resolve() != target or target.suffix.lower() != '.exe':
        raise UpdateError('更新目标必须是原 EXE 的绝对路径')
    if 'target_directory' in plan:
        if stage.parent != updates_directory().resolve() or target.parent != Path(plan['target_directory']):
            raise UpdateError('更新目标或暂存目录与原计划不一致')
    elif target.parent != stage.parent:
        # 2.1.0's already-running helper uses its original sidecar plan.
        raise UpdateError('旧版更新计划与原 EXE 目录不一致')
    if any((stage / name).is_symlink() for name in ('new.exe', 'previous.exe', 'ack.json', 'result.json')):
        raise UpdateError('更新目录中的文件路径无效')
    for key in ('sha256', 'original_sha256'):
        if not re.fullmatch(r'[a-f0-9]{64}', plan[key]):
            raise UpdateError('更新计划的校验值无效')
    if type(plan['parent_pid']) is not int or plan['parent_pid'] <= 0 or not re.fullmatch(r'[a-f0-9]{48}', plan['nonce']):
        raise UpdateError('更新计划的进程信息无效')
    context = plan.get('diagnostics')
    plan['diagnostics'] = {key: value for key, value in (context.items() if isinstance(context, dict) else [])
                           if key in ('operation_id', 'parent_id', 'root_id') and isinstance(value, str)
                           and re.fullmatch(r'[a-f0-9]{32}', value)}
    if 'local_test' in plan:
        local = plan['local_test']
        if not isinstance(local, dict) or set(local) != {'origin', 'current_version'}:
            raise UpdateError('本地更新测试计划无效')
        LocalUpdateSource(local['origin'], local['current_version'])
    return manifest, stage, target, plan


def _spawn(arguments, directory):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('_PYI_')}
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return subprocess.Popen(arguments, cwd=directory, env=environment, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)


@traced('update.launch_helper', level='INFO')
def launch_update(manifest, executable=None):
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        raise UpdateError('自更新适用于 Windows 便携 EXE；源码运行请从发布页面下载')
    manifest, stage, target, plan = _load_plan(manifest)
    helper = stage / 'updater.exe'
    shutil.copy2(executable or sys.executable, helper)
    return _spawn([str(helper), '--apply-update', str(manifest)], target.parent)


def _wait_for_parent(pid, seconds=60):
    if sys.platform != 'win32':
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means the process has already exited.
        if ctypes.get_last_error() == 87:
            return
        raise UpdateError('无法等待原程序退出，原程序已保留')
    try:
        if kernel.WaitForSingleObject(handle, int(seconds*1000)) != 0:
            raise UpdateError('等待原程序退出超时，原程序已保留')
    finally:
        kernel.CloseHandle(handle)


def _replace(source, target, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        try:
            replace_file(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _wait_for_ack(stage, nonce, process, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            ack = json.loads((stage / 'ack.json').read_text(encoding='utf-8'))
            if ack.get('nonce') == nonce:
                return True
        except (OSError, ValueError):
            pass
        if process.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def _stop_restarted_process(process):
    if process.poll() is None:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        else:
            process.terminate()
        process.wait(timeout=15)


def apply_update(manifest) -> int:
    """Invoked in the copied helper, before importing Qt or opening a window."""
    manifest, stage, target, plan = _load_plan(manifest)
    configure_logging(initial_log_level(data_directory() / 'LCSC3D-settings.json'),
                      version=plan.get('version'), role='update')
    with log_context(plan.get('diagnostics', {}), update_id=stage.name):
        return _apply_update(manifest, stage, target, plan)


@traced('update.install', level='INFO')
def _apply_update(manifest, stage, target, plan):
    backup = stage / 'previous.exe'
    moved = False
    process = None
    phase = 'wait_for_exit'
    source = LocalUpdateSource(**plan['local_test']) if 'local_test' in plan else None
    try:
        log_event('INFO', 'update.install_stage', stage=phase)
        _wait_for_parent(plan['parent_pid'])
        phase = 'verify_before_replace'
        log_event('INFO', 'update.install_stage', stage=phase)
        if file_hash(stage / 'new.exe') != plan['sha256'] or file_hash(target) != plan['original_sha256']:
            raise UpdateError('更新文件或原程序在准备后发生了变化，更新已中止')
        phase = 'backup'
        log_event('INFO', 'update.install_stage', stage=phase)
        _replace(target, backup)
        moved = True
        phase = 'replace'
        log_event('INFO', 'update.install_stage', stage=phase)
        _replace(stage / 'new.exe', target)
        phase = 'restart'
        log_event('INFO', 'update.install_stage', stage=phase)
        restart_args = source.arguments(restarted=True) if source is not None else []
        process = _spawn([str(target), '--update-ack', str(manifest), *restart_args], target.parent)
        phase = 'confirm_startup'
        log_event('INFO', 'update.install_stage', stage=phase)
        if not _wait_for_ack(stage, plan['nonce'], process):
            raise UpdateError('新版启动失败，正在恢复原程序')
        result = {'status': 'success', 'version': plan['version'], 'helper_pid': os.getpid(), 'new_pid': process.pid}
        log_event('INFO', 'update.install_result', status='success')
        code = 0
    except Exception as exc:
        error = str(exc)
        record_error(exc, 'update.install_failed', stage=phase)
        result = {'status': 'failed', 'error_type': type(exc).__name__, 'error_id': getattr(exc, '_diagnostic_id', None),
                  'stage': phase, 'helper_pid': os.getpid()}
        if moved:
            try:
                log_event('WARNING', 'update.rollback_started', failed_stage=phase)
                if process is not None:
                    _stop_restarted_process(process)
                _replace(backup, target)
                _spawn([str(target), *(source.arguments() if source is not None else [])], target.parent)
                log_event('INFO', 'update.rollback_completed')
                result['rollback'] = 'success'
            except Exception as rollback_error:
                record_error(rollback_error, 'update.rollback_failed', level='CRITICAL')
                result['rollback'] = 'failed'
                error += f'；自动恢复未完成，原程序保留在 {backup}：{rollback_error}'
        result['error'] = '更新失败，详见 LCSC3D-update.log（' + phase + '）。'
        code = 1
    (stage / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if code:
        _show_failure(error)
    return code


def _show_failure(message):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'LCSC3D 更新失败', 0x10)


def discard_update(manifest):
    _, stage, _, _ = _load_plan(manifest)
    if not (stage / 'previous.exe').exists() and not (stage / 'result.json').exists():
        shutil.rmtree(stage)


def acknowledge_update(manifest, executable=None):
    _, stage, target, plan = _load_plan(manifest)
    if target != Path(executable or sys.executable).resolve() or file_hash(target) != plan['sha256']:
        raise UpdateError('新版启动确认与更新计划不一致')
    temporary = stage / 'ack.tmp'
    temporary.write_text(json.dumps({'nonce': plan['nonce'], 'pid': os.getpid()}), encoding='ascii')
    os.replace(temporary, stage / 'ack.json')
    with log_context(plan.get('diagnostics', {}), update_id=stage.name):
        log_event('INFO', 'update.startup_acknowledged')


def cleanup_updates(directory):
    """A later startup removes only completed updates; failed backups stay."""
    directory = Path(directory).resolve()
    for stage in directory.glob(STAGE_PREFIX + '*'):
        if stage.is_symlink() or not stage.is_dir() or stage.resolve().parent != directory:
            continue
        try:
            result = json.loads((stage / 'result.json').read_text(encoding='utf-8'))
            if result.get('status') == 'success':
                shutil.rmtree(stage)
                log_event('DEBUG', 'update.stage_cleaned', update_id=stage.name)
        except (OSError, ValueError) as exc:
            # Running helpers are locked on Windows; a subsequent startup retries.
            record_error(exc, 'update.cleanup_deferred', level='DEBUG')
