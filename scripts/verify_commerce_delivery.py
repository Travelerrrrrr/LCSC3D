"""Run the frozen shopping workflow in isolated app data and a clean working directory."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def verify(exe, root):
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Use a fresh verification directory')
    application, cwd = root / 'application', root / 'working-directory'
    destination = root / 'LocalAppData/LCSC3D/verification'
    for folder in (application, cwd, destination, root / 'LocalAppData/LCSC3D/temp', root / 'AppData'):
        folder.mkdir(parents=True, exist_ok=True)
    executable = application / 'LCSC3D.exe'
    shutil.copyfile(exe, executable)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'QT_', 'QML_', 'QSG_', '_PYI_', 'LCSC3D_'))}
    windows = Path(os.environ['SystemRoot'])
    environment.update(LOCALAPPDATA=str(root / 'LocalAppData'), APPDATA=str(root / 'AppData'),
                       TEMP=str(root / 'LocalAppData/LCSC3D/temp'), TMP=str(root / 'LocalAppData/LCSC3D/temp'),
                       PATH=os.pathsep.join(map(str, (windows / 'System32', windows))))
    started = time.monotonic()
    with (destination / 'stdout.log').open('wb') as out, (destination / 'stderr.log').open('wb') as err:
        process = subprocess.run([str(executable), '--self-test-commerce', str(destination)],
                                 cwd=cwd, env=environment, stdout=out, stderr=err,
                                 timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    report = json.loads((destination / 'commerce-verification.json').read_text('utf-8'))
    assert process.returncode == 0 and report['success'] and report['frozen'], report
    for flag in ('context_download_and_quantity_sync', 'inline_add_without_navigation', 'no_storefront_webview',
                 'native_cart_quantity_saved', 'external_cart_without_checkout', 'native_order_list',
                 'quantity_increments_and_price_tiers',
                 'batch_rejection_continues', 'per_item_cart_results',
                 'favorite_rules_checked_before_write',
                 'quantity_preserved_between_tabs', 'logout_clears_account_tables',
                 'logs_package_and_continue_after_clear', 'single_native_window'):
        assert report[flag], flag
    assert {p.name for p in application.iterdir()} == {'LCSC3D.exe'}
    assert not any(cwd.iterdir())
    assert executable.read_bytes() == exe.read_bytes()
    summary = dict(success=True, version=report['version'], seconds=round(time.monotonic() - started, 2),
                   clean_exe_and_cwd=True, system_path_only=True, report=str(destination / 'commerce-verification.json'))
    (root / 'delivery-verification.json').write_text(json.dumps(summary, indent=2), 'utf-8')
    print(json.dumps(summary))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--verify-dir', type=Path, required=True)
    args = parser.parse_args()
    verify(args.exe.resolve(), args.verify_dir)
