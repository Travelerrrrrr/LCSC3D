"""Verify native KiCad parsing, rendering and physical geometry using isolated data."""
import argparse
from collections import Counter
import gzip
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from lcsc3d.eda.easyeda_import import parse_component
from lcsc3d.eda.eda_ir import Pad, Pin, Via
from lcsc3d.eda.kicad import build_library, FORMATS, pin_electrical_type


def expected_bbox(pad):
    if pad.vertices:
        xs, ys = [float(v.x) for v in pad.vertices], [-float(v.y) for v in pad.vertices]
        return [min(xs), min(ys), max(xs), max(ys)]
    w, h = float(pad.size.x), float(pad.size.y)
    a = math.radians(float(pad.rotation))
    c, s = abs(math.cos(a)), abs(math.sin(a))
    if pad.shape == 'rect':
        rx, ry = (w*c+h*s)/2, (w*s+h*c)/2
    elif pad.shape == 'oval':
        r = min(w, h) / 2
        rx, ry = (r + (w-h)/2*c, r + (w-h)/2*s) if w >= h else (r + (h-w)/2*s, r + (h-w)/2*c)
    elif pad.shape == 'ellipse':
        rx, ry = math.hypot(w*c, h*s)/2, math.hypot(w*s, h*c)/2
    else:
        raise AssertionError('Need an independent bound for ' + pad.shape)
    x, y = float(pad.copper_center.x), -float(pad.copper_center.y)
    return [x-rx, y-ry, x+rx, y+ry]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--python', type=Path, help='KiCad bundled Python for independent pcbnew geometry checks')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(out / 'LocalAppData'), APPDATA=str(out / 'AppData'),
               KICAD_CONFIG_HOME=str(out / 'config'), PYTHONUTF8='1')
    os.environ['LOCALAPPDATA'] = env['LOCALAPPDATA']
    def run(arguments, name):
        result = subprocess.run([str(args.cli.resolve()), *arguments], env=env, text=True,
                                encoding='utf-8', errors='replace', capture_output=True, timeout=120)
        (out / (name + '.log')).write_text(result.stdout + result.stderr, 'utf-8')
        assert result.returncode == 0, name + ': ' + result.stderr
        assert not re.search(r'^(?:Error\b|Failed to create file)', result.stdout + result.stderr, re.M | re.I), result.stdout + result.stderr
        return result.stdout.strip()
    version = run(['version'], 'version')
    fixtures = ROOT / 'app/tests/fixtures'
    cases = json.loads(gzip.decompress((fixtures / 'official_ad_cases.json.gz').read_bytes()))['cases']
    cases += [{'part': p, 'data': json.loads((fixtures / (p + '.json')).read_text('utf-8'))}
              for p in ('C23922', 'C8734', 'C5879483', 'C31966')]
    components = [parse_component(c['data'], c['part']) for c in cases]
    by_part = {c.part: c for c in components}
    artifacts, errors = build_library(components, {c.part: FORMATS for c in components}, 'LCSC3D_Test')
    assert not errors, errors
    manifest = []
    for artifact in artifacts:
        target = out / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.payload)
        if artifact.format == 'KICAD_FOOTPRINT':
            source = by_part[artifact.parts[0]]
            manifest.append({'name': target.stem, 'library': str(target.parent), 'pads': [
                {'number': p.number, 'position': [float(p.center.x), -float(p.center.y)],
                 'drill': float(p.drill), 'slot': float(p.slot_length),
                 'drill_rotation': float(p.drill_rotation), 'plated': p.plated, 'layer': p.layer,
                 'bbox': expected_bbox(p)}
                if isinstance(p, Pad) else
                {'number': '', 'position': [float(p.center.x), -float(p.center.y)],
                 'drill': float(p.drill), 'slot': 0, 'drill_rotation': 0, 'plated': True, 'layer': 'all_copper',
                 'bbox': [float(p.center.x-p.diameter/2),float(-p.center.y-p.diameter/2),
                          float(p.center.x+p.diameter/2),float(-p.center.y+p.diameter/2)]}
                for p in source.footprint.items if isinstance(p, (Pad,Via))]})
    manifest_path = out / 'geometry-expected.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    (out / 'symbol-svg').mkdir(exist_ok=True)
    (out / 'footprint-svg').mkdir(exist_ok=True)
    run(['sym', 'export', 'svg', '--output', str(out / 'symbol-svg'), str(out / 'LCSC3D_Test.kicad_sym')], 'symbols')
    run(['fp', 'export', 'svg', '--output', str(out / 'footprint-svg'),
         '--layers', 'F.Cu,B.Cu,F.Mask,F.SilkS,F.Fab,Edge.Cuts', str(out / 'LCSC3D_Test.pretty')], 'footprints')
    units = sum(len(c.symbol.units) for c in components)
    assert len(list((out / 'symbol-svg').glob('*.svg'))) == units
    assert len(list((out / 'footprint-svg').glob('*.svg'))) == len(manifest)
    # Native parse + save is independent of our writer and our test S-expression reader.
    (out / 'native-resaved.kicad_sym').unlink(missing_ok=True)
    run(['sym', 'upgrade', '--force', '--output', str(out / 'native-resaved.kicad_sym'),
         str(out / 'LCSC3D_Test.kicad_sym')], 'roundtrip')
    sys.path.insert(0, str(ROOT / 'app/tests'))
    from test_kicad import sexpr, children, child
    symbols = children(sexpr((out / 'native-resaved.kicad_sym').read_bytes()), 'symbol')
    expected_pin_counts = Counter()
    for c in components:
        expected_pin_counts[c.part] = sum(isinstance(i, Pin) for u in c.symbol.units for i in u.items)
    actual_pin_counts = Counter()
    for symbol in symbols:
        properties = {row[1]: row[2] for row in children(symbol, 'property')}
        actual_pin_counts[properties['SupplierPart']] = sum(len(children(u, 'pin')) for u in children(symbol, 'symbol'))
        component = by_part[properties['SupplierPart']]
        source_pins = {(u.number, i.number): i for u in component.symbol.units for i in u.items if isinstance(i, Pin)}
        def hidden(node):
            return 'hide' in node or any(row[1] == 'yes' for row in children(node, 'hide'))
        names_hidden = any(hidden(node) for node in children(symbol, 'pin_names'))
        numbers_hidden = any(hidden(node) for node in children(symbol, 'pin_numbers'))
        for unit in children(symbol, 'symbol'):
            unit_number = int(unit[1].rsplit('_', 2)[1])
            for pin in children(unit, 'pin'):
                source = source_pins[(unit_number, child(pin, 'number')[1])]
                at = child(pin, 'at')
                length = float(child(pin, 'length')[1])
                angle = math.radians(float(at[3]))
                assert math.isclose(float(at[1]), float(source.connection.x), abs_tol=0.000001)
                assert math.isclose(float(at[2]), float(source.connection.y), abs_tol=0.000001)
                assert math.isclose(float(at[1])+math.cos(angle)*length, float(source.body.x), abs_tol=0.000001)
                assert math.isclose(float(at[2])+math.sin(angle)*length, float(source.body.y), abs_tol=0.000001)
                assert pin[1] == pin_electrical_type(source.electrical), (component.part, source.number, pin[1])
                for label, globally_hidden, expected_visible in (
                        ('name', names_hidden, source.name_visible), ('number', numbers_hidden, source.number_visible)):
                    text = child(pin, label)
                    size = float(child(child(child(text, 'effects'), 'font'), 'size')[1])
                    assert (not globally_hidden and size > 0) == expected_visible, (component.part, source.number, label)
                assert child(pin, 'name')[1] == source.name
            for rectangle in children(unit, 'rectangle'):
                assert child(child(rectangle, 'fill'), 'type')[1] == 'background'
            source_unit = next(u for u in component.symbol.units if u.number == unit_number)
            assert len(children(unit, 'rectangle')) == sum(getattr(i,'kind','') == 'rect' for i in source_unit.items)
        ref = properties['Footprint']
        assert ref.startswith('LCSC3D_Test:') and (out / 'LCSC3D_Test.pretty' / (ref.split(':', 1)[1] + '.kicad_mod')).is_file()
    assert actual_pin_counts == expected_pin_counts
    geometry = None
    if args.python:
        result = subprocess.run([str(args.python.resolve()), str(ROOT / 'scripts/verify_kicad_geometry.py'),
                                 str(manifest_path), str(out / 'geometry-native.json')], env=env,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        (out / 'geometry.log').write_text(result.stdout + result.stderr, 'utf-8')
        assert result.returncode == 0, result.stdout + result.stderr
        geometry = json.loads((out / 'geometry-native.json').read_text('utf-8'))
    report = dict(success=True, version=version, components=len(components), symbol_units=units,
                  footprints=len(manifest), pins=sum(actual_pin_counts.values()), native_geometry=geometry,
                  pin_label_visibility_verified=True, electrical_types_verified=True,
                  rectangle_background_fill_verified=True, square_rectangles_verified=True,
                  pin_connections_and_directions_verified=True, no_user_profile=True)
    (out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
