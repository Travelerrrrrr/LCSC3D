"""Verify the frozen model pipeline with isolated data and only the Windows PATH.

Supply public C2765186.json and source.step in --fixtures.
This deliberately does not use cached credentials or network fixtures.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def verify(exe,fixtures,root):
    root = root.resolve()
    executable = root/'application/LCSC3D.exe'
    destination = root/'LocalAppData/LCSC3D/verification'
    cwd = root/'working-directory'
    for directory in (executable.parent,destination/'fixtures',cwd,root/'Temp',root/'AppData'):
        directory.mkdir(parents=True,exist_ok=True)
    if any(cwd.iterdir()) or any(executable.parent.iterdir()):
        raise ValueError('Use a fresh verification directory')
    shutil.copy2(exe,executable)
    for name in ('C2765186.json','source.step'):
        shutil.copyfile(fixtures/name,destination/'fixtures'/name)
    environment = {k:v for k,v in os.environ.items()
                   if not k.startswith(('PYTHON','QT_','QML_','QSG_','_PYI_','LCSC3D_'))}
    windows = Path(os.environ['SystemRoot'])
    environment.update(PATH=os.pathsep.join(map(str,(windows/'System32',windows))),
        LOCALAPPDATA=str(root/'LocalAppData'),APPDATA=str(root/'AppData'),
        TEMP=str(root/'Temp'),TMP=str(root/'Temp'))
    started = time.monotonic()
    with (destination/'stdout.log').open('wb') as stdout, (destination/'stderr.log').open('wb') as stderr:
        process = subprocess.run([str(executable),'--self-test-models',str(destination)],
            cwd=cwd,env=environment,stdout=stdout,stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,timeout=360)
    report = json.loads((destination/'model-verification.json').read_text('utf-8'))
    assert process.returncode==0 and report['success'],report.get('error')
    assert report['frozen'] and len(report['cases'])==9
    assert all(c['success'] and c['model_instances']>0 for c in report['cases'])
    assert not any(cwd.iterdir()),'Unexpected files in current working directory'
    assert {p.name for p in executable.parent.iterdir()}=={'LCSC3D.exe'},'Unexpected files next to EXE'
    assert not list((root/'LocalAppData/LCSC3D/temp').glob('model-geometry-*')),'Worker temporary directory leaked'
    assert not (root/'LocalAppData/LCSC3D/cache/step-bounds-v1').exists(),'Unexpected geometry cache'
    assert executable.read_bytes()==exe.read_bytes()
    summary = {'success':True,'version':report['version'],'frozen':True,
        'cases':len(report['cases']),'seconds':round(time.monotonic()-started,2),
        'system_path_only':True,'clean_exe_and_cwd':True,'no_geometry_cache':True,
        'detailed_report':str(destination/'model-verification.json')}
    (root/'delivery-verification.json').write_text(json.dumps(summary,indent=2),'utf-8')
    print(json.dumps(summary))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--verify-dir',type=Path,required=True)
    args = parser.parse_args()
    verify(args.exe.resolve(),args.fixtures.resolve(),args.verify_dir)
