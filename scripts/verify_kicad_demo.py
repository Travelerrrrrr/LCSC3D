"""Verify the frozen KiCad demo through its actual window, with an offline API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--exe', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--cli', type=Path)
args = parser.parse_args()
root = args.output.resolve()
root.mkdir(parents=True, exist_ok=True)
fixtures = root / 'fixtures'
fixtures.mkdir(exist_ok=True)
for part in ('C20197', 'C2765186', 'C23922', 'C8734', 'C2040', 'C5879483', 'C31966'):
    shutil.copyfile(ROOT / 'app/tests/fixtures' / (part + '.json'), fixtures / (part + '.json'))
shutil.copyfile(ROOT / 'app/tests/fixtures/C2040_svgs.json', fixtures / 'C2040_svgs.json')
shutil.copyfile(ROOT / 'app/tests/fixtures/C5879483_svgs.json', fixtures / 'C5879483_svgs.json')
env = os.environ.copy()
env.update(LOCALAPPDATA=str(root / 'LocalAppData'), APPDATA=str(root / 'AppData'),
           TEMP=str(root / 'Temp'), TMP=str(root / 'Temp'), KICAD_CONFIG_HOME=str(root / 'kicad-config'), PYTHONUTF8='1')
(root / 'Temp').mkdir(exist_ok=True)
executable = args.exe.resolve()
with (root / 'process.log').open('w', encoding='utf-8') as output:
    process = subprocess.run([str(executable), '--self-test-kicad', str(root)], env=env, stdout=output,
                             stderr=subprocess.STDOUT, timeout=120)
assert process.returncode == 0, process.returncode
report = json.loads((root / 'kicad-verification.json').read_text('utf-8'))
version = re.search(r"VERSION = '([^']+)'", (ROOT / 'app/lcsc3d/ui/main.py').read_text('utf-8')).group(1)
assert report['success'] and report['frozen'] and report['version'] == version, report
assert [c['name'] for c in report['cases']] == ['individual', 'combined', 'merge-symbols', 'merge-footprints', 'footprint-only', 'copies', 'reversed', 'ad', 'ad-layout', 'ad-replace', 'replace', 'via-ad', 'via-kicad', 'preview-sample']
assert report['kicad_preview_verified']
assert report['preview_painted_pixels_verified']
assert report['reversed_pin_preview_verified'] and report['wrapped_results_and_card_spacing_verified']
assert report['merge_transition_frames_verified']
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'app/tests')]
from altium_inspect import schematic, pcb
ad = next(c for c in report['cases'] if c['name'] == 'ad')
for name in ad['files']:
    path = Path(name)
    pins = 8 if 'C20197' in str(path) else 2 if 'C5879483' in str(path) else 14
    if path.suffix == '.SchLib':
        assert len(schematic(path.read_bytes())[2]) == pins
        if pins == 2:
            actual = {p['number']:p for p in schematic(path.read_bytes())[2]}
            assert [(actual[n]['x'],actual[n]['length'],actual[n]['flags']&3) for n in ('1','2')] == [(-10,20,2),(10,20,0)]
    if path.suffix == '.PcbLib':
        assert len(pcb(path.read_bytes())[1]) == (18 if pins == 14 else pins)
from test_kicad import sexpr, children, child
from lcsc3d.eda.easyeda_import import parse_component
from lcsc3d.eda.eda_ir import Pin
from lcsc3d.eda.kicad import pin_electrical_type
assert schematic((root/'ad-layout/lcsc3d_v2_3_0_hb.SchLib').read_bytes())[0]['COMPCOUNT'] == '12'
assert schematic((root/'ad-replace/lcsc3d_v2_3_0_hb.SchLib').read_bytes())[0]['COMPCOUNT'] == '1'
assert len(children(sexpr((root/'replace/LCSC3D.kicad_sym').read_bytes()),'symbol')) == 1
via_ad = pcb((root/'via-ad/lcsc3d_v2_3_0_hb.PcbLib').read_bytes())
assert len(via_ad[1]) == 9 and sum(kind==3 for kind,_ in via_ad[2]) == 4
import struct
assert all(struct.unpack_from('<iiBB',data,21)==(240000,120000,1,32) for kind,data in via_ad[2] if kind==3)
via_fp = next((root/'via-kicad/LCSC3D.pretty').glob('*.kicad_mod'))
via_pads = children(sexpr(via_fp.read_bytes()),'pad')
assert len(via_pads)==13 and sum(p[2]=='thru_hole' for p in via_pads)==4
report['via_geometry_verified'] = True
for case in report['cases']:
    for name in case['files']:
        path = Path(name)
        if path.suffix != '.kicad_sym':
            continue
        for symbol in children(sexpr(path.read_bytes()), 'symbol'):
            props = {p[1]: p[2] for p in children(symbol, 'property')}
            component = parse_component(json.loads((fixtures / (props['SupplierPart'] + '.json')).read_text('utf-8')), props['SupplierPart'])
            source = {(u.number, p.number): p for u in component.symbol.units for p in u.items if isinstance(p, Pin)}
            for unit in children(symbol, 'symbol'):
                number = int(unit[1].rsplit('_', 2)[1])
                for pin in children(unit, 'pin'):
                    expected = pin_electrical_type(source[(number, child(pin, 'number')[1])].electrical,
                        'input' if case['name'] == 'merge-symbols' else 'source')
                    assert pin[1] == expected, (case['name'], pin, expected)
            ref = props['Footprint']
            library, footprint = ref.split(':')
            assert len(list((root / case['name']).rglob(library + '.pretty/' + footprint + '.kicad_mod'))) == 1
for name, symbols, footprints in (('combined', 1, 1), ('merge-symbols', 1, 2), ('merge-footprints', 2, 1), ('footprint-only', 0, 1), ('copies',3,3)):
    assert len(list((root / name).rglob('*.kicad_sym'))) == symbols
    assert len(list((root / name).rglob('*.pretty'))) == footprints
if args.cli:
    for kind, target in (('sym', root / 'combined/LCSC3D.kicad_sym'), ('fp', root / 'combined/LCSC3D.pretty')):
        (root / ('native-' + kind)).mkdir(exist_ok=True)
        command = [str(args.cli.resolve()), kind, 'export', 'svg', '--output', str(root / ('native-' + kind)), str(target)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        (root / ('native-' + kind + '.log')).write_text(result.stdout + result.stderr, 'utf-8')
        assert result.returncode == 0 and 'Error:' not in result.stderr, result.stderr
    expected_units = sum(len(json.loads((fixtures / (p + '.json')).read_text('utf-8')).get('subparts') or [None])
                         for p in ('C23922', 'C8734'))
    assert len(list((root / 'native-sym').glob('*.svg'))) == expected_units
    assert len(list((root / 'native-fp').glob('*.svg'))) == 1
    report['frozen_files_read_by_kicad'] = True
report['ad_geometry_verified'] = True
report['electrical_types_and_references_verified'] = True
report['exe_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
(root / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps({k: v for k, v in report.items() if k != 'cases'}, ensure_ascii=False))
