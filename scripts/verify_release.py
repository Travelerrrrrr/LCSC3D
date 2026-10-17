"""Gate a Windows release on isolated frozen-app acceptance and source equality."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, default=ROOT / 'app/dist/LCSC3D.exe')
    parser.add_argument('--verify-dir', type=Path, default=ROOT / 'work/release-verification',
                        help='Fresh isolated directory for this acceptance run')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Frozen acceptance requires Windows')
    executable = args.exe.resolve(strict=True)
    work = args.verify_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    outputs = ROOT / 'outputs'
    outputs.mkdir(exist_ok=True)
    if executable != (outputs / 'LCSC3D.exe').resolve():
        shutil.copyfile(executable, outputs / 'LCSC3D.exe')
    version = re.search(r"^VERSION = '([^']+)'", (ROOT / 'app/lcsc3d/ui/main.py').read_text('utf-8'), re.M)[1]
    report = dict(version=version, source_commit=os.environ.get('GITHUB_SHA', ''), cases=[], errors=[], success=False)
    environment = os.environ.copy()
    environment['LOCALAPPDATA'] = str(work / 'driver-profile')
    environment['PYTHONPATH'] = str(ROOT / 'app')

    def run(label, command, timeout=360):
        started = time.monotonic()
        print('VERIFY', label, flush=True)
        with (work / (label + '.log')).open('wb') as log:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            print((work / (label + '.log')).read_text('utf-8', errors='replace')[-12000:], flush=True)
            raise RuntimeError(f'{label} failed: {result.returncode}')
        report['cases'].append(dict(name=label, seconds=round(time.monotonic() - started, 2)))

    def script(name, *arguments, timeout=360):
        run(name, [sys.executable, str(ROOT / 'scripts' / (name + '.py')), *map(str, arguments)], timeout)

    def attempt(name, operation):
        try:
            operation()
        except Exception as error:
            report['errors'].append(dict(name=name, error=str(error)))
            traceback.print_exc()

    def frozen(name, flag, filename, timeout=180):
        case = work / name
        application, cwd = case / 'application', case / 'cwd'
        destination = case / 'LocalAppData/LCSC3D/verification'
        for folder in (application, cwd, destination, case / 'AppData'):
            folder.mkdir(parents=True, exist_ok=False)
        copy = application / 'LCSC3D.exe'
        shutil.copyfile(executable, copy)
        shutil.copytree(ROOT / 'app/tests/fixtures', destination / 'fixtures')
        env = {k: v for k, v in environment.items() if not k.startswith(('PYTHON', 'QT_', 'QML_', 'QSG_', '_PYI_', 'LCSC3D_'))}
        windows = Path(os.environ['SystemRoot'])
        temporary = case / 'LocalAppData/LCSC3D/temp'
        temporary.mkdir(parents=True)
        env.update(LOCALAPPDATA=str(case / 'LocalAppData'), APPDATA=str(case / 'AppData'),
                   TEMP=str(temporary), TMP=str(temporary), PATH=os.pathsep.join(map(str, (windows / 'System32', windows))))
        started = time.monotonic()
        print('VERIFY frozen', name, flush=True)
        with (case / 'stdout.log').open('wb') as log:
            result = subprocess.run([str(copy), flag, str(destination)], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        report_path = destination / filename
        if result.returncode or not report_path.is_file():
            # A native crash may happen before the self-test writes its JSON.
            # Preserve the exit code and crash stack instead of masking it as
            # a missing-file error in the CI job's main log.
            for log in (case / 'stdout.log', *sorted((destination / 'logs').glob('LCSC3D-crash-*.log'))):
                if log.is_file():
                    print(log.read_text('utf-8', errors='replace')[-12000:], flush=True)
            raise RuntimeError(f'{name} exited {result.returncode} (0x{result.returncode & 0xffffffff:08X}); '
                               f'report_present={report_path.is_file()}')
        data = json.loads(report_path.read_text('utf-8'))
        assert result.returncode == 0 and data['success'] and data['frozen'], data
        assert data['version'] == version, data
        assert not any(cwd.iterdir()), name
        assert {p.name for p in application.iterdir()} == {'LCSC3D.exe'}, name
        assert copy.read_bytes() == executable.read_bytes(), name
        copy.unlink()
        report['cases'].append(dict(name=name, seconds=round(time.monotonic() - started, 2),
                                    clean_exe_and_cwd=True, system_path_only=True))

    try:
        for name, flag, filename in (
            ('settings-logs', '--self-test-settings', 'settings-verification.json'),
            ('backups', '--self-test-backups', 'backups-verification.json'),
            ('export-controls', '--self-test-export', 'export-verification.json'),
            ('kicad-ad', '--self-test-kicad', 'kicad-verification.json'),
            ('store', '--self-test-store', 'favorites-verification.json'),
            ('material-ui', '--self-test-material', 'material-verification.json'),
            ('live-resources', '--self-test-resources', 'verification.json'),
            ('live-catalog', '--self-test-catalog-live', 'catalog-live-verification.json')):
            attempt(name, lambda: frozen(name, flag, filename))
        attempt('commerce', lambda: script('verify_commerce_delivery', '--exe', executable, '--verify-dir', work / 'commerce'))
        attempt('progress', lambda: script('verify_progress_delivery', '--exe', executable, '--verify-dir', work / 'progress'))
        # Public model bytes are downloaded once, then all placement/export cases
        # run offline. No saved account or login is used.
        def models():
            sys.path.insert(0, str(ROOT / 'app'))
            from lcsc3d.services.backend import NetworkApi
            from lcsc3d.models.model3d import model_reference
            model_fixtures = work / 'model-fixtures'
            model_fixtures.mkdir()
            source = ROOT / 'app/tests/fixtures/C2765186.json'
            shutil.copyfile(source, model_fixtures / source.name)
            reference = model_reference(json.loads(source.read_text('utf-8')))
            api = NetworkApi(use_cache=False)
            (model_fixtures / 'source.step').write_bytes(api.get_step_3d_model(reference.uuid))
            (model_fixtures / 'source.obj').write_text(api.get_raw_3d_model_obj(reference.uuid), 'utf-8')
            script('verify_model_delivery', '--exe', executable, '--fixtures', model_fixtures, '--verify-dir', work / 'models')
        attempt('models', models)
        attempt('portable', lambda: script('verify_portable', '--output-dir', outputs, '--verify-dir', work / '便携验证'))
        attempt('app-data', lambda: script('verify_app_data', '--output-dir', outputs, '--verify-dir', work / 'app-data'))
        for scenario in ('success', 'bad-checksum', 'no-update', 'check-failure'):
            attempt('update-' + scenario, lambda: run('update-' + scenario,
                [sys.executable, str(ROOT / 'scripts/local_update_test.py'), '--exe', str(executable), '--auto', '--scenario', scenario]))
        attempt('update-startup', lambda: run('update-startup', [sys.executable, str(ROOT / 'scripts/local_update_test.py'),
                                                               '--exe', str(executable), '--auto', '--startup']))
        if report['errors']:
            raise RuntimeError('Frozen acceptance failed: ' + ', '.join(row['name'] for row in report['errors']))
        script('package_source', '--output-dir', outputs)
        script('verify_bundle', '--exe', outputs / 'LCSC3D.exe', '--source-zip', outputs / 'LCSC3D.zip',
               '--report', work / 'bundle-verification.json')
        hashes = []
        for name in ('LCSC3D.exe', 'LCSC3D.zip'):
            with (outputs / name).open('rb') as stream:
                hashes.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + name)
        (outputs / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', 'ascii')
        report['success'] = True
    finally:
        (work / 'release-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
