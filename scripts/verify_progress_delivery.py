"""Verify progress/messages in a copied EXE with isolated app data and system PATH."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def verify(exe, root):
    root=root.resolve()
    application=root/'application'
    cwd=root/'working-directory'
    if root.exists() and any(root.iterdir()):
        raise ValueError('Use a fresh verification directory')
    for folder in (application,cwd,root/'LocalAppData/LCSC3D/temp',root/'AppData'):
        folder.mkdir(parents=True,exist_ok=True)
    executable=application/'LCSC3D.exe'
    shutil.copyfile(exe,executable)
    destination=root/'LocalAppData/LCSC3D/verification'
    destination.mkdir(parents=True)
    environment={k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','QT_','QML_','QSG_','_PYI_','LCSC3D_'))}
    windows=Path(os.environ['SystemRoot'])
    environment.update(LOCALAPPDATA=str(root/'LocalAppData'),APPDATA=str(root/'AppData'),
        TEMP=str(root/'LocalAppData/LCSC3D/temp'),TMP=str(root/'LocalAppData/LCSC3D/temp'),
        PATH=os.pathsep.join(map(str,(windows/'System32',windows))))
    started=time.monotonic()
    with (destination/'stdout.log').open('wb') as stdout,(destination/'stderr.log').open('wb') as stderr:
        process=subprocess.run([str(executable),'--self-test-progress',str(destination)],cwd=cwd,
            env=environment,stdout=stdout,stderr=stderr,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((destination/'progress-verification.json').read_text('utf-8'))
    assert process.returncode==0 and report['success'] and report['frozen'],report
    for flag in ('full_after_commit','newest_top_right','older_moves_down','history_saved_reloaded',
                 'messages_page_and_copy','progress_and_notification_logs','logs_packaged_and_writable_after_clear',
                 'message_history_survives_log_clear','single_native_window'):
        assert report[flag],flag
    assert len(report['download_intermediate_values'])>3
    assert {p.name for p in application.iterdir()}=={'LCSC3D.exe'}
    assert not any(cwd.iterdir())
    assert executable.read_bytes()==exe.read_bytes()
    summary=dict(success=True,version=report['version'],seconds=round(time.monotonic()-started,2),
        clean_exe_and_cwd=True,system_path_only=True,report=str(destination/'progress-verification.json'))
    (root/'delivery-verification.json').write_text(json.dumps(summary,indent=2),'utf-8')
    print(json.dumps(summary))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--verify-dir',type=Path,required=True)
    args=parser.parse_args()
    verify(args.exe.resolve(),args.verify_dir)
