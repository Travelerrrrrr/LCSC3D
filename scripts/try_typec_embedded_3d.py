"""Generate and audit one experimental C2765186 PcbLib with an embedded STEP.

This is a development probe, not a production exporter. Only the fixture's
verified zero-rotation, unit-scale placement is admitted. STEP geometry is read
with OpenCascade; no CAD dependency is added to the application. Native record
framing is checked against OriginalCircuit.Altium/AltiumSharp (Apache-2.0),
and KiCad's Altium importer: https://github.com/issus/AltiumSharp and
https://docs.kicad.org/doxygen/altium__pcb_8cpp_source.html .
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
PART = 'C2765186'
MM_PER_RAW = 0.00000254


def parameters(values):
    pairs = values.items() if isinstance(values, dict) else values
    raw = '|'.join(f'{key}={value}' for key, value in pairs).encode('ascii') + b'\0'
    return struct.pack('<I', len(raw)) + raw


def read_parameters(raw):
    size, = struct.unpack_from('<I', raw)
    assert size + 4 == len(raw), 'parameter frame length'
    return dict(field.split('=', 1) for field in raw[4:].rstrip(b'\0').decode('ascii').split('|') if '=' in field)


def mil(value):
    return format(Decimal(str(value)) / Decimal('0.0254'), '.9f') + 'mil'


def native_raw(value):
    return int(round(value / MM_PER_RAW))


def bounds(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    low, high = box.CornerMin(), box.CornerMax()
    return [low.X(), low.Y(), low.Z(), high.X(), high.Y(), high.Z()]


def load_shape(path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone, 'STEP read failed'
    assert reader.TransferRoots(), 'STEP contains no roots'
    shape = reader.OneShape()
    assert not shape.IsNull(), 'STEP contains no shape'
    return shape


def derive_placement(source, box):
    package = source['packageDetail']['dataStr']
    if isinstance(package, str):
        package = json.loads(package)
    nodes = [json.loads(s.partition('~')[2])['attrs'] for s in package['shape'] if s.startswith('SVGNODE~')]
    assert len(nodes) == 1 and nodes[0]['uuid'] == '4ee8413127e64716b804db03d4b340ae', 'unexpected model association'
    node = nodes[0]
    rotation = tuple(float(v) for v in node['c_rotation'].split(','))
    assert rotation == (0., 0., 0.), 'this probe does not establish nonzero rotation semantics'
    assert node.get('transform') == 'scale(1) translate(0, 0)', 'non-unit source transform'
    ox, oy = (float(v) for v in node['c_origin'].split(','))
    fx, fy = float(package['head']['x']), float(package['head']['y'])
    center = [(box[0] + box[3]) / 2, (box[1] + box[4]) / 2]
    source_offset = [(ox - fx) * .254, (fy - oy) * .254, float(node['z']) * .254]
    # The source places a model normalized to its XY projected center and low Z.
    # Negative source Z preserves protruding pins; it is not clamped to zero.
    translation = [source_offset[0] - center[0], source_offset[1] - center[1], source_offset[2] - box[2]]
    dimensions = [box[3] - box[0], box[4] - box[1]]
    declared = [float(node['c_width']) * .254, float(node['c_height']) * .254]
    assert all(abs(a - b) < .005 for a, b in zip(dimensions, declared)), 'STEP and source projected dimensions disagree'
    return {'source_model': node['uuid'], 'source_offset_mm': source_offset,
            'step_projected_center_mm': center, 'step_bounds_mm': box,
            'translation_mm': translation, 'rotation_degrees': rotation,
            'source_declared_dimensions_mm': declared}


def embed(base, step, translation, box, model_name):
    import olefile
    import altium
    from compound_storage import compound_file
    with olefile.OleFileIO(io.BytesIO(base)) as ole:
        streams = {'/'.join(path): ole.openstream(path).read() for path in ole.listdir()}
    section = next(path.split('/')[0] for path in streams if path.endswith('/Data') and not path.startswith('Library/'))
    model_id = '{' + str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(step).hexdigest())).upper() + '}'
    low = [box[i] + translation[i] for i in range(3)]
    high = [box[i+3] + translation[i] for i in range(3)]
    fields = [
        ('V7_LAYER', 'MECHANICAL1'), ('NAME', ''), ('KIND', 0), ('SUBPOLYINDEX', -1), ('UNIONINDEX', 0),
        ('ARCRESOLUTION', '0.5mil'), ('ISSHAPEBASED', 'FALSE'), ('CAVITYHEIGHT', '0mil'),
        ('STANDOFFHEIGHT', mil(low[2])), ('OVERALLHEIGHT', mil(high[2])), ('BODYPROJECTION', 0),
        ('ARCRESOLUTION', '0.5mil'), ('BODYCOLOR3D', 8421504), ('BODYOPACITY3D', '1.000'),
        ('IDENTIFIER', ''), ('TEXTURE', ''), ('TEXTURECENTERX', '0mil'), ('TEXTURECENTERY', '0mil'),
        ('TEXTURESIZEX', '0mil'), ('TEXTURESIZEY', '0mil'), ('TEXTUREROTATION', '0'),
        ('MODELID', model_id), ('MODEL.CHECKSUM', 0), ('MODEL.EMBED', 'TRUE'), ('MODEL.NAME', model_name),
        ('MODEL.2D.X', mil(translation[0])), ('MODEL.2D.Y', mil(translation[1])), ('MODEL.2D.ROTATION', '0.000'),
        ('MODEL.3D.ROTX', '0.000'), ('MODEL.3D.ROTY', '0.000'), ('MODEL.3D.ROTZ', '0.000'),
        ('MODEL.3D.DZ', mil(translation[2])), ('MODEL.MODELTYPE', 1),
    ]
    outline = [(low[0], low[1]), (high[0], low[1]), (high[0], high[1]), (low[0], high[1])]
    body = altium._common(57) + bytes(5) + parameters(fields) + struct.pack('<I', len(outline))
    body += b''.join(struct.pack('<dd', native_raw(x), native_raw(y)) for x, y in outline)
    streams[section + '/Data'] += b'\x0c' + struct.pack('<I', len(body)) + body
    count, = struct.unpack('<I', streams[section + '/Header'])
    streams[section + '/Header'] = struct.pack('<I', count + 1)
    streams['Library/Models/Header'] = struct.pack('<I', 1)
    streams['Library/Models/Data'] = parameters({'EMBED': 'TRUE', 'MODELSOURCE': 'Undefined', 'ID': model_id,
        'ROTX': '0.000', 'ROTY': '0.000', 'ROTZ': '0.000', 'DZ': 0, 'CHECKSUM': 0, 'NAME': model_name})
    streams['Library/Models/0'] = zlib.compress(step)
    return compound_file(streams)


def audit_library(base, result, original_step):
    import olefile
    sys.path.insert(0, str(ROOT / 'app/tests'))
    from altium_inspect import pcb, Reader, parameters as decode_parameters
    base_name, base_pads, base_primitives = pcb(base)
    name, pads, primitives = pcb(result)
    assert name == base_name and pads == base_pads, '2D pads changed'
    assert primitives[:-1] == base_primitives and primitives[-1][0] == 12, '2D primitives changed'
    body = primitives[-1][1]
    reader = Reader(body)
    header = reader.read(18)
    assert header[0] == 57, 'body is not on Mechanical 1'
    params = decode_parameters(reader.block()[1])
    vertex_count, = reader.unpack('<I')
    assert vertex_count == 4
    outline = [reader.unpack('<dd') for _ in range(vertex_count)]
    assert reader.offset == len(body), 'trailing body bytes'
    with olefile.OleFileIO(io.BytesIO(result), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        assert ole.openstream('Library/Models/Header').read() == struct.pack('<I', 1)
        metadata = read_parameters(ole.openstream('Library/Models/Data').read())
        assert metadata['ID'] == params['MODELID'] and metadata['EMBED'] == params['MODEL.EMBED'] == 'TRUE'
        assert metadata['CHECKSUM'] == params['MODEL.CHECKSUM'] == '0'
        extracted = zlib.decompress(ole.openstream('Library/Models/0').read())
        assert extracted == original_step, 'embedded STEP differs from source'
    translation = [float(params[key].removesuffix('mil')) * .0254 for key in ('MODEL.2D.X', 'MODEL.2D.Y', 'MODEL.3D.DZ')]
    assert params['BODYPROJECTION'] == '0' and params['MODEL.MODELTYPE'] == '1'
    return {'footprint_name': name, 'pads_and_holes': len(pads), 'unchanged_2d_primitives': len(base_primitives),
            'embedded_model_id': metadata['ID'], 'embedded_step_sha256': hashlib.sha256(extracted).hexdigest(),
            'translation_mm': translation, 'outline_raw': outline,
            'checks': ['2D pads and primitives unchanged', 'one 3D Body', 'one linked embedded model',
                       'embedded STEP byte-identical to input', 'no external model reference']}, extracted, pads


def audit_mounting(shape, pads, translation):
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane
    groups = defaultdict(list)
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face(faces.Current())
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() == GeomAbs_Plane and abs(surface.Plane().Axis().Direction().Z()) > .99999:
            b = bounds(face)
            if .15 < b[3]-b[0] < .5 and .4 < b[4]-b[1] < .8:
                groups[round(b[2], 5)].append(b)
        faces.Next()
    top_pads = sorted((p for p in pads if p['layer'] == 1), key=lambda p: p['x'])
    assert len(top_pads) == 12
    # Independently locate a horizontal solder-contact row in the actual STEP.
    candidates = [(z, row) for z, row in groups.items() if len(row) == len(top_pads)
                  and max(b[4] for b in row)-min(b[4] for b in row) < .01]
    assert len(candidates) == 1, 'ambiguous STEP mounting contact plane'
    z, row = candidates[0]
    row.sort(key=lambda b: b[0])
    matches = []
    for contact, pad in zip(row, top_pads):
        x, y, w, h = [pad[k] * MM_PER_RAW for k in ('x', 'y', 'width', 'height')]
        moved = [contact[i] + translation[i % 3] for i in range(6)]
        margin = min(moved[0] - (x-w/2), (x+w/2) - moved[3], moved[1] - (y-h/2), (y+h/2) - moved[4])
        assert margin > -.0002, f'contact outside pad {pad["number"]}'
        assert abs(moved[2]) < .005, 'contact does not touch board plane'
        matches.append({'pad': pad['number'], 'contact_bounds_after_placement_mm': moved, 'minimum_xy_margin_mm': margin})
    # Two plastic locator tips: detect the horizontal faces below the solder row.
    pin_faces = []
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face(faces.Current())
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() == GeomAbs_Plane and abs(surface.Plane().Axis().Direction().Z()) > .99999:
            b = bounds(face)
            if z-1 < b[2] < z-.4 and .25 < b[3]-b[0] < .4 and .25 < b[4]-b[1] < .4:
                pin_faces.append(b)
        faces.Next()
    holes = sorted((p for p in pads if not p['number']), key=lambda p: p['x'])
    assert len(holes) == 2
    locator_errors = []
    for hole in holes:
        matching = [b for b in pin_faces if math.hypot(
            (b[0]+b[3])/2 + translation[0] - hole['x']*MM_PER_RAW,
            (b[1]+b[4])/2 + translation[1] - hole['y']*MM_PER_RAW) < .05]
        assert matching, 'no STEP locator tip at the footprint hole'
        face = min(matching, key=lambda b: b[2])
        dx = (face[0]+face[3])/2 + translation[0] - hole['x']*MM_PER_RAW
        dy = (face[1]+face[4])/2 + translation[1] - hole['y']*MM_PER_RAW
        error = math.hypot(dx, dy)
        assert error < .05, 'locator does not align with its hole'
        locator_errors.append(error)
    b = bounds(shape)
    return {'step_contact_plane_z_mm': z, 'placed_contact_plane_z_mm': z+translation[2],
            'lowest_placed_geometry_z_mm': b[2]+translation[2], 'contact_count': len(matches),
            'contact_pad_matches': matches, 'locator_hole_errors_mm': locator_errors,
            'checks': ['12 solder contact faces contained in corresponding copper pads',
                       'solder contact plane within 0.005 mm of Z=0', 'two locators within 0.05 mm of hole centers',
                       'negative-Z insertion geometry preserved']}


def render(shape, pads, translation, path):
    from app_paths import data_directory
    mpl_cache = data_directory() / 'cache/matplotlib'
    mpl_cache.mkdir(parents=True, exist_ok=True)
    os.environ['MPLCONFIGDIR'] = str(mpl_cache)
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    from OCP.TopLoc import TopLoc_Location
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Rectangle, Circle
    mesh = BRepMesh_IncrementalMesh(shape, .04, False, .2, True)
    mesh.Perform()
    triangles = []
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face(faces.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is not None:
            for index in range(1, triangulation.NbTriangles()+1):
                nodes = triangulation.Triangle(index).Get()
                points = [triangulation.Node(i).Transformed(location.Transformation()) for i in nodes]
                triangles.append([[p.X(), p.Y(), p.Z()] for p in points])
        faces.Next()
    raw = np.array(triangles)
    fig, axes = plt.subplots(2, 2, figsize=(13, 10), dpi=160)
    fig.suptitle('C2765186 Type-C: STEP origin and mounting-plane alignment', fontsize=16)
    for column, shift in enumerate(([0., 0., 0.], translation)):
        moved = raw + np.array(shift)
        top, side = axes[0, column], axes[1, column]
        top.add_collection(PolyCollection(moved[:, :, :2], facecolors='#a9b8c8', edgecolors='#40536c', linewidths=.10, alpha=.12))
        side.add_collection(PolyCollection(moved[:, :, [1, 2]], facecolors='#a9b8c8', edgecolors='#40536c', linewidths=.10, alpha=.15))
        for pad in pads:
            x, y, w, h = [pad[k] * MM_PER_RAW for k in ('x', 'y', 'width', 'height')]
            if pad['number']:
                top.add_patch(Rectangle((x-w/2, y-h/2), w, h, facecolor='#ffc244', edgecolor='#7b4b00', linewidth=.8, alpha=.7))
                side.plot([y-h/2, y+h/2], [0, 0], color='#b56d00', linewidth=3)
            else:
                top.add_patch(Circle((x, y), pad['drill']*MM_PER_RAW/2, fill=False, edgecolor='#a92f83', linewidth=1.5))
        top.plot(0, 0, 'k+', markersize=15, markeredgewidth=2, label='Footprint origin')
        top.plot(shift[0], shift[1], 'rx', markersize=9, markeredgewidth=2, label='STEP origin after placement')
        side.axhline(0, color='#168768', linewidth=1.5, label='Mounting board plane Z=0')
        top.set(xlim=(-5.5, 5.5), ylim=(-5.5, 5), xlabel='X (mm)', ylabel='Y (mm)',
                title=('Control: original STEP coordinates' if column == 0 else 'Aligned: original STEP + placement transform'))
        side.set(xlim=(-5.5, 5), ylim=(-3, 4), xlabel='Y (mm)', ylabel='Z (mm)',
                 title=('Side: contacts below board plane' if column == 0 else 'Side: solder contacts at Z=0; insertion pins below'))
        for ax in (top, side):
            ax.set_aspect('equal')
            ax.grid(alpha=.18)
            ax.legend(loc='upper left', fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, .96))
    fig.savefig(path)
    plt.close(fig)


def native_roundtrip(executable, output, report):
    """Open/save only our copied library in a new, isolated Altium process."""
    from app_paths import data_directory
    from altium_inspect import Reader, parameters as decode_parameters
    import olefile
    inventory = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq X2.EXE', '/FO', 'CSV', '/NH'],
                               capture_output=True, check=True, timeout=15)
    if b'"x2.exe"' in inventory.stdout.lower():
        raise RuntimeError('Close the existing Altium instance before this isolated native probe')
    folder = data_directory() / 'verification/typec-embed/native'
    folder.mkdir(parents=True, exist_ok=True)
    library = folder / 'roundtrip.PcbLib'
    library.write_bytes((output / 'C2765186-TypeC-3D.PcbLib').read_bytes())
    log = folder / 'native-ad.txt'
    log.unlink(missing_ok=True)
    pas_path = lambda path: str(path).replace("'", "''")
    script = """procedure ProbeTypeC;
var Report : TStringList; Doc : IServerDocument; Lib : IPCB_Library;
begin
    Report := TStringList.Create;
    Report.Add('started');
    Report.SaveToFile('%s');
    Doc := Client.OpenDocument('PCBLIB', '%s');
    if Doc = nil then begin
        Report.Add('open_failed'); Report.SaveToFile('%s');
        Report.Free; TerminateWithExitCode(1); Exit;
    end;
    Client.ShowDocument(Doc);
    Lib := PCBServer.GetCurrentPCBLibrary;
    if Lib = nil then Report.Add('pcb_server_failed')
    else Report.Add('components=' + IntToStr(Lib.ComponentCount));
    if Doc.DoFileSave('') then Report.Add('saved') else Report.Add('save_failed');
    Report.SaveToFile('%s'); Report.Free; TerminateWithExitCode(0);
end;
""" % (pas_path(log), pas_path(library), pas_path(log), pas_path(log))
    (folder / 'NativeProbe.pas').write_text(script, 'utf-8')
    project = folder / 'NativeProbe.PrjScr'
    project.write_text('[Design]\nVersion=1.0\n\n[Document1]\nDocumentPath=NativeProbe.pas\n', 'utf-8')
    env = os.environ.copy()
    # Keep the external CAD application's profile isolated as well. Reuse that
    # verification profile for subsequent launches rather than touching the
    # user's real Altium login/configuration files.
    env['APPDATA'] = str(Path(os.environ['LOCALAPPDATA']).parent / 'RoamingAppData')
    Path(env['APPDATA']).mkdir(exist_ok=True)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    command = '-RScriptingSystem:RunScript(ProjectName="' + str(project) + '"|ProcName="ProbeTypeC")'
    # Altium parses the raw -R command line itself. Passing a Python argv list
    # inserts backslash-escaped quotes which its RunScript parser does not admit.
    # This is CreateProcess with shell=False; the generated paths are arguments,
    # never PowerShell/cmd code.
    native_exe = str(executable.resolve())
    process = subprocess.Popen('"' + native_exe + '" ' + command, executable=native_exe, env=env, cwd=folder, startupinfo=startup,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for attempt in range(3):
            try:
                code = process.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                print(f'Native AD probe is still running ({(attempt+1)*30}s)', flush=True)
        else:
            raise TimeoutError('Native AD did not finish the probe within 90 seconds')
        assert code == 0 and log.exists(), 'Native AD script did not complete'
        stages = log.read_text().splitlines()
        assert stages == ['started', 'components=1', 'saved'], stages
        raw = library.read_bytes()
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            section = report['library_audit']['footprint_name']
            reader = Reader(ole.openstream([section, 'Data']).read())
            name = Reader(reader.block()[1]).string()
            kinds, bodies = [], []
            while reader.offset < len(reader.data):
                kind, = reader.unpack('<B')
                assert kind in (1, 2, 3, 4, 5, 6, 11, 12), 'unverified native primitive'
                kinds.append(kind)
                blocks = [reader.block()[1] for _ in range(6 if kind == 2 else 2 if kind == 5 else 1)]
                if kind == 12:
                    body = Reader(blocks[0])
                    body.read(18)
                    bodies.append(decode_parameters(body.block()[1]))
            count, = struct.unpack('<I', ole.openstream([section, 'Header']).read())
            assert count == len(kinds) and kinds.count(2) == 18 and len(bodies) == 1
            model = decode_parameters(Reader(ole.openstream('Library/Models/Data').read()).block()[1])
            extracted = zlib.decompress(ole.openstream('Library/Models/0').read())
            assert model['ID'] == bodies[0]['MODELID']
            assert hashlib.sha256(extracted).hexdigest() == report['library_audit']['embedded_step_sha256']
            assert int(model['CHECKSUM']) & 0xffffffff == int(bodies[0]['MODEL.CHECKSUM']) & 0xffffffff
            translation = [float(bodies[0][k].removesuffix('mil')) * .0254 for k in ('MODEL.2D.X', 'MODEL.2D.Y', 'MODEL.3D.DZ')]
            assert max(abs(a-b) for a, b in zip(translation, report['library_audit']['translation_mm'])) < .00001
        target = output / 'C2765186-TypeC-3D-AD26.PcbLib'
        target.write_bytes(raw)
        result = {'engine': 'Altium Designer 26', 'stages': stages, 'footprint': name,
                  'pads_and_holes': kinds.count(2), '3d_bodies': len(bodies), 'translation_mm': translation,
                  'model_checksum_signed': model['CHECKSUM'], 'body_checksum_unsigned': bodies[0]['MODEL.CHECKSUM'],
                  'embedded_step_sha256': hashlib.sha256(extracted).hexdigest(),
                  'file_sha256': hashlib.sha256(raw).hexdigest(),
                  'checks': ['Altium native library open/save completed', '18 pads/holes and one body preserved',
                             'native placement preserved within 0.00001 mm', 'STEP bytes preserved exactly',
                             'native model/body checksums agree modulo 32 bits']}
        (output / 'native-ad-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--step', type=Path, required=True, help='Official associated C2765186 STEP')
    parser.add_argument('--cad-package-dir', type=Path, required=True, help='Development-only OpenCascade Python package directory')
    parser.add_argument('--profile', type=Path, required=True, help='Isolated LOCALAPPDATA for this verification')
    parser.add_argument('--output', type=Path, required=True, help='Selected model/library output directory')
    parser.add_argument('--altium-exe', type=Path, help='Optional installed X2.EXE for an isolated native open/save audit')
    args = parser.parse_args()
    os.environ['LOCALAPPDATA'] = str(args.profile.resolve())
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT / 'app'), str(args.cad_package_dir.resolve())]
    from app_paths import configure_runtime_paths, data_directory
    configure_runtime_paths()
    from altium import export_pcblib, export_schlib
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = json.loads((ROOT / 'app/tests/fixtures/C2765186.json').read_text('utf-8'))
    step = args.step.read_bytes()
    assert b'ISO-10303-21' in step[:256] and b'END-ISO-10303-21' in step[-256:]
    shape = load_shape(args.step.resolve())
    placement = derive_placement(source, bounds(shape))
    base = export_pcblib(source, PART)
    result = embed(base, step, placement['translation_mm'], placement['step_bounds_mm'], PART + '.step')
    audit, extracted, pads = audit_library(base, result, step)
    temp = data_directory() / 'verification/typec-embed'
    temp.mkdir(parents=True, exist_ok=True)
    extracted_path = temp / 'extracted-embedded.step'
    extracted_path.write_bytes(extracted)
    geometry = audit_mounting(load_shape(extracted_path), pads, audit['translation_mm'])
    aligned_path = output / 'C2765186-TypeC-3D.PcbLib'
    aligned_path.write_bytes(result)
    symbol_path = output / 'C2765186-TypeC.SchLib'
    symbol_path.write_bytes(export_schlib(source, PART))
    (output / 'C2765186-control-original-origin.PcbLib').write_bytes(embed(base, step, [0., 0., 0.], placement['step_bounds_mm'], PART + '.step'))
    image = output / 'alignment.png'
    render(shape, pads, audit['translation_mm'], image)
    report = {'part': PART, 'experimental': True, 'source_fixture_sha256': hashlib.sha256((ROOT / 'app/tests/fixtures/C2765186.json').read_bytes()).hexdigest(),
              'placement': placement, 'library_audit': audit, 'geometry_audit': geometry,
              'files': {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in (aligned_path, symbol_path, image)},
              'limitations': ['Zero rotation and unit scale only', 'AD internal MODEL.CHECKSUM remains 0 until native save; SHA-256 verifies the exact STEP bytes',
                              'AD GUI display, rotation and board-side flip require native acceptance; independent parsing and CAD checks do not replace it',
                              'This probe does not implement model merge/append or production UI']}
    # Preserve a current geometry report even if native launch needs sign-in.
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    if args.altium_exe:
        report['native_ad_audit'] = native_roundtrip(args.altium_exe, output, report)
        report['limitations'][2] = 'Native open/save passed; AD 3D viewport display, rotation and board-side flip are not yet visually accepted'
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'output': str(aligned_path), 'translation_mm': audit['translation_mm'],
                      'contact_plane_z_mm': geometry['placed_contact_plane_z_mm'],
                      'lowest_z_mm': geometry['lowest_placed_geometry_z_mm'],
                      'contacts': geometry['contact_count'], 'locator_errors_mm': geometry['locator_hole_errors_mm']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
