"""Gate publication on checksums, executable acceptance and the exact Git source."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = {'LCSC3D.exe', 'LCSC3D.zip'}
REQUIRED_CASES = {
    'settings-logs', 'pdf-downloads', 'backups', 'export-controls', 'kicad-ad', 'store',
    'material-ui', 'live-resources', 'live-catalog', 'verify_commerce_delivery',
    'verify_progress_delivery', 'verify_model_delivery', 'verify_portable', 'verify_app_data',
    'update-success', 'update-bad-checksum', 'update-no-update', 'update-check-failure',
    'update-startup', 'package_source', 'verify_bundle',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(directory, version, commit, *, repository=ROOT):
    directory, repository = Path(directory), Path(repository)
    require(re.fullmatch(r'\d+\.\d+\.\d+', version), 'Invalid version')
    require(re.fullmatch(r'[0-9a-f]{40}', commit), 'Invalid source commit')
    checksums = {}
    for line in (directory / 'SHA256SUMS.txt').read_text('ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (LCSC3D\.exe|LCSC3D\.zip)', line)
        require(match is not None, 'Invalid checksum entry')
        digest, name = match.groups()
        require(name not in checksums, 'Duplicate checksum entry')
        with (directory / name).open('rb') as stream:
            require(hashlib.file_digest(stream, 'sha256').hexdigest() == digest, 'Checksum mismatch: ' + name)
        checksums[name] = digest
    require(set(checksums) == ASSETS, 'Incomplete checksum manifest')
    with (directory / 'LCSC3D.exe').open('rb') as stream:
        require(stream.read(2) == b'MZ', 'Invalid Windows executable')

    evidence = json.loads((directory / 'release-verification.json').read_text('utf-8'))
    require(evidence.get('success') is True and evidence.get('errors') == [], 'Executable acceptance failed')
    require(evidence.get('source_commit') == commit and evidence.get('version') == version,
            'Acceptance does not match the selected commit/version')
    require(REQUIRED_CASES <= {row.get('name') for row in evidence.get('cases', [])}, 'Missing acceptance cases')
    for filename, flags in (
        ('pdf-verification.json', ('pdf_only', 'missing_report_exact', 'missing_notice', 'mixed_exports',
                                   'cancel_restores_pdf_and_report', 'cancel_progress_zero',
                                   'retry_and_stale_report_cleanup', 'diagnostics_verified')),
        ('progress-verification.json', ('cancel_progress_animated_to_zero', 'cancel_removes_new_and_restores_previous',
                                       'retry_after_cancel', 'logs_packaged_and_writable_after_clear')),
    ):
        result = json.loads((directory / filename).read_text('utf-8'))
        require(result.get('success') is True and result.get('frozen') is True
                and result.get('version') == version and all(result.get(flag) is True for flag in flags),
                'Incomplete frozen verification: ' + filename)
        if filename.startswith('progress'):
            values = result.get('cancel_progress_values', [])
            require(values and all(type(value) is int for value in values) and values[0] > 0
                    and values[-1] == 0 and values == sorted(values, reverse=True) and len(set(values)) > 3,
                    'Cancellation animation was not verified')

    def git(*args, input=None):
        return subprocess.check_output(['git', '-C', str(repository), *args], input=input)
    require(git('rev-parse', 'HEAD').decode().strip() == commit, 'Checkout differs from verified commit')
    source_version = re.search(r"^VERSION = '([^']+)'", git('show', commit + ':app/lcsc3d/ui/main.py').decode('utf-8'), re.M)
    require(source_version and source_version[1] == version, 'Application version differs from release')
    blobs = {}
    for entry in git('ls-tree', '-rz', commit).split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        _, kind, sha = metadata.split()
        require(kind == b'blob', 'Unexpected non-file source entry')
        blobs[path.decode('utf-8')] = sha.decode()
    with zipfile.ZipFile(directory / 'LCSC3D.zip') as archive:
        require(archive.testzip() is None, 'Source ZIP is corrupt')
        seen = set()
        for name in archive.namelist():
            require(name.startswith('LCSC3D/'), 'Invalid source ZIP root')
            relative = name.removeprefix('LCSC3D/')
            require(relative and not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts,
                    'Source ZIP path escapes the repository')
            require(relative not in seen and relative in blobs, 'Unexpected or duplicate source file: ' + relative)
            # Apply this commit's Git text/binary rules, preserving PDF bytes and allowing checkout EOLs.
            actual = git('hash-object', '--stdin', '--path=' + relative, input=archive.read(name)).decode().strip()
            require(actual == blobs[relative], 'Source differs from verified commit: ' + relative)
            seen.add(relative)
        require(seen == set(blobs), 'Source ZIP does not contain every tracked file')
    return dict(success=True, version=version, source_commit=commit, source_files=len(blobs), sha256=checksums)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = verify(args.directory, args.version, args.commit)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=False))
