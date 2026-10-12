"""3D placement, independent resource verification and complete library output."""
import base64
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import backend
from app_logging import close_logging
from app_paths import temporary_directory
from easyeda_import import parse_component, model_outline_center
from eda_ir import Model3D, ModelPlacement, ModelAsset, dumps, loads
from errors import DownloadError, Cancelled
from model_export import resolve_models
from model_geometry import inspect_models, scaled_step, rotation_zxy, euler_xyz, _distance_squared, _scale_units
from altium import export_pcblib, MergedLibrary
from library_merge import LibraryIndex, append_library
from kicad import build_library, footprint_definition
from library_files import commit_library
import olefile
import zstandard

FIXTURES = Path(__file__).parent/'fixtures'
OBJ = '''v 10 20 30
v 12 20 30
v 12 24 30
v 10 24 30
v 10 20 33
v 12 20 33
v 12 24 33
v 10 24 33
f 1 2 3 4
f 5 8 7 6
f 1 5 6 2
f 2 6 7 3
f 3 7 8 4
f 4 8 5 1
'''


def source(angles='0,0,0', z='0'):
    data = json.loads((FIXTURES/'C20197.json').read_text('utf-8'))
    doc = data['packageDetail']['dataStr']
    if isinstance(doc,str):
        doc = data['packageDetail']['dataStr'] = json.loads(doc)
    attrs = {'c_etype':'outline3D','uuid':'test-model','title':'box','id':'model-one',
             'c_origin':str(doc['head']['x'])+','+str(doc['head']['y']),
             'c_width':str(D(2)/D('.254')),'c_height':str(D(4)/D('.254')),
             'c_rotation':angles,'z':z,'transform':'scale(1) translate(0, 0)'}
    doc['shape'] = [s for s in doc['shape'] if not s.startswith('SVGNODE~')] + ['SVGNODE~'+json.dumps({'attrs':attrs})]
    return data


def edit_attrs(data, **changes):
    shapes = data['packageDetail']['dataStr']['shape']
    node = json.loads(shapes[-1].split('~',1)[1])
    node['attrs'].update(changes)
    shapes[-1] = 'SVGNODE~'+json.dumps(node)
    return data


class ModelPlacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=temporary_directory())
        cls.folder = Path(cls.directory.name)
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs
        from OCP.IFSelect import IFSelect_RetDone
        writer = STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(-1,-2,-.5),2,4,3).Shape(),STEPControl_AsIs)
        path = cls.folder/'box.step'
        assert writer.Write(str(path)) == IFSelect_RetDone
        cls.step = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=self.folder)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ,LOCALAPPDATA=str(self.root/'profile'))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)

    def resolved(self, data=None, *, ad=False):
        c = parse_component(data or source(),'C20197')
        return resolve_models(c,lambda ref:backend.StepPayload(self.step,identifier=ref.uuid),lambda _:OBJ,prepare_altium=ad)

    def api(self, data=None):
        api = backend.NetworkApi(threading.Event(),use_cache=False)
        api.get_cad_data_of_component = lambda part:data or source()
        api.get_step_3d_model = lambda identifier:self.step
        api.get_raw_3d_model_obj = lambda identifier:OBJ
        api.get_3d_library_model = lambda identifier:None
        return api

    def test_official_typec_and_rotated_source_parameters_are_preserved(self):
        c = parse_component(json.loads((FIXTURES/'C2765186.json').read_text('utf-8')),'C2765186')
        p = c.models[0].placement
        self.assertEqual(p.position,(D(0),D('-1.1999976'),D('-.8999982')))
        self.assertEqual(p.angles,(D(0),)*3)
        self.assertEqual(loads(dumps(c)),c)
        for name in ('ir_v1_0.json','ir_v1_1_via.json'):
            old = loads((FIXTURES/name).read_text('utf-8'))
            self.assertEqual(loads(dumps(old)),old)

    def test_standard_pcb_placement_rebases_stale_public_model_origins(self):
        cases = json.loads((FIXTURES/'model_origin_cases.json').read_text('utf-8'))['cases']
        for case in cases:
            with self.subTest(part=case['part']):
                before = json.dumps(case['component'],ensure_ascii=False)
                c = parse_component(case['component'],case['part'])
                self.assertIsNotNone(c.footprint)
                self.assertTrue(any(d.code=='model_origin_normalized' and d.severity=='info' for d in c.diagnostics))
                p = c.models[0].placement
                self.assertLess(max(abs(v) for v in p.position[:2]),D('.0001'))
                self.assertEqual(p.angles[2],{'C2861386':D(0),'C57113':D(90),'C83790':D(180)}[case['part']])
                raw = json.loads(c.models[0].extensions[0].payload)['placements'][0]['c_origin']
                self.assertNotEqual(raw,'4000,3000')
                self.assertEqual(json.dumps(case['component'],ensure_ascii=False),before)
                self.assertEqual(loads(dumps(c)),c)

    def test_c2861386_resolved_export_has_no_spurious_far_placement(self):
        case = json.loads((FIXTURES/'model_origin_cases.json').read_text('utf-8'))['cases'][0]
        c,assets = self.resolved(case['component'])
        self.assertTrue(assets,c.diagnostics)
        self.assertEqual(c.models[0].placement.position,(D(0),D(0),D(0)))
        self.assertFalse(any(d.code=='model_far_from_footprint' for d in c.diagnostics))
        self.assertEqual((c.models[0].transform[3],c.models[0].transform[7]),(D(0),D(0)))
        text = footprint_definition(c,'CorrectOrigin',model_assets=assets).decode()
        offset = tuple(map(D,re.search(r'\(offset \(xyz ([^)]+)\)',text).group(1).split()))
        self.assertEqual(offset[:2],(D(0),D(0)))

    def test_outline_origin_preserves_deliberate_offset_and_all_outline_children(self):
        data = source('10,20,30','-2')
        doc = data['packageDetail']['dataStr']
        node = json.loads(doc['shape'][-1].partition('~')[2])
        x,y = D(str(doc['head']['x'])),D(str(doc['head']['y']))
        # The model outline is deliberately offset from the footprint. Its own
        # display scaling does not participate in SVG getBBox on that group.
        node['attrs']['transform'] = 'scale(2) translate(123, 456)'
        node['childNodes'] = [{'nodeName':'polyline','attrs':{'points':f'{x+48} {y-32} {x+49} {y-31}'}},
                              {'nodeName':'polyline','attrs':{'points':f'{x+51} {y-29} {x+52} {y-28}'}}]
        doc['shape'][-1] = 'SVGNODE~'+json.dumps(node)
        c = parse_component(data,'C20197')
        p = c.models[0].placement
        self.assertEqual(p.position,(D('12.7'),D('7.62'),D('-.508')))
        self.assertEqual(p.angles,(D(10),D(20),D(30)))

    def test_unknown_outline_geometry_does_not_guess_a_new_origin(self):
        for child in ({'nodeName':'path','attrs':{'d':'M 0 0 L 10 10'}},
                      {'nodeName':'polyline','attrs':{'points':'0 0 10 10','transform':'translate(5,6)'}},
                      {'nodeName':'polyline','attrs':{'points':'nan 0 10 10'}},
                      {'nodeName':'polyline','attrs':{'points':'0 0 10 0'}}):
            self.assertIsNone(model_outline_center({'childNodes':[child]}))
        self.assertIsNone(model_outline_center({'childNodes':[]}))

    def test_all_explicit_nodes_survive_and_malformed_node_keeps_2d(self):
        data = source()
        doc = data['packageDetail']['dataStr']
        doc['shape'].append(doc['shape'][-1].replace('model-one','model-two'))
        c = parse_component(data,'C20197')
        self.assertEqual(len(c.models),2)
        bad = parse_component(edit_attrs(source(),c_rotation='1,2',z='nan'),'C20197')
        self.assertIsNotNone(bad.footprint)
        self.assertTrue(any(d.domain=='model3d' and d.severity=='error' for d in bad.diagnostics))
        self.assertIsNone(bad.models[0].placement)

    def test_legacy_head_only_link_is_not_a_zero_placement(self):
        data = source()
        doc = data['packageDetail']['dataStr']
        doc['shape'] = [s for s in doc['shape'] if not s.startswith('SVGNODE~')]
        doc['head']['uuid_3d'] = 'legacy-library'
        c = parse_component(data,'C20197')
        self.assertEqual(c.models[0].identifier,'legacy-library')
        self.assertIsNone(c.models[0].placement)
        self.assertTrue(any(d.code=='missing_model_placement' for d in c.diagnostics))

    def test_legacy_or_corrupt_model_nodes_do_not_destroy_2d_geometry(self):
        data = source()
        shapes = data['packageDetail']['dataStr']['shape']
        node = json.loads(shapes[-1].split('~',1)[1])
        del node['attrs']['c_etype']
        shapes[-1] = 'SVGNODE~'+json.dumps(node)
        c = parse_component(data,'C20197')
        self.assertIsNotNone(c.footprint)
        self.assertIsNotNone(c.models[0].placement)
        self.assertFalse(any(d.domain=='footprint' for d in c.diagnostics))
        shapes[-1] = 'SVGNODE~{broken'
        c = parse_component(data,'C20197')
        self.assertIsNotNone(c.footprint)
        self.assertTrue(any(d.domain=='model3d' and d.severity=='error' for d in c.diagnostics))

    def test_missing_dimensions_and_nonfinite_vectors_never_default_to_zero(self):
        for updates in ({'c_width':''},{'c_width':'0'},{'c_origin':'nan,0'},{'c_rotation':'0,inf,0'},{'z':''}):
            with self.subTest(updates=updates):
                c = parse_component(edit_attrs(source(),**updates),'C20197')
                self.assertIsNone(c.models[0].placement)
                self.assertTrue(c.diagnostics)

    def test_triangle_distance_checks_surface_not_vertex_density(self):
        self.assertAlmostEqual(_distance_squared((.2,.2,1),(0,0,0),(1,0,0),(0,1,0)),1)
        self.assertAlmostEqual(_distance_squared((.2,.2,0),(0,0,0),(1,0,0),(0,1,0)),0)
        self.assertAlmostEqual(_distance_squared((2,0,0),(0,0,0),(1,0,0),(0,1,0)),1)

    def test_cad_worker_accepts_geometry_with_different_file_origins(self):
        result = inspect_models(self.step,OBJ)
        self.assertTrue(result['compatible'],result)
        self.assertAlmostEqual(result['bounds'][2],-.5,places=6)
        self.assertEqual(result['obj_bounds'],[10.,20.,30.,12.,24.,33.])

    def test_coordinate_mismatch_skips_model_but_keeps_footprint(self):
        c = parse_component(source(),'C20197')
        different = OBJ.replace('12 24','16 24').replace('12 20','16 20')
        c,assets = resolve_models(c,lambda _:backend.StepPayload(self.step),lambda _:different)
        self.assertFalse(assets)
        self.assertFalse(c.models[0].transform)
        self.assertIsNotNone(c.footprint)
        self.assertIn('几何基准',c.diagnostics[-1].message)

    def test_ad_unrepresentable_resource_origin_skips_only_the_model(self):
        # Local geometry can fit even when its translation cannot be encoded
        # in AD's raw int32 coordinate fields. Reject before serializing 2D.
        geometry = {'compatible':True,'bounds':[9999,-2,-.5,10001,2,2.5],
                    'obj_bounds':[10,20,30,12,24,33]}
        with patch('model_export.inspect_models',return_value=geometry):
            c,assets = self.resolved(ad=True)
        self.assertFalse(assets)
        self.assertIsNotNone(c.footprint)
        self.assertFalse(c.models[0].transform)
        self.assertIn('AD 坐标范围',c.diagnostics[-1].message)

    def test_source_zxy_rotation_and_negative_z_have_expected_affine_mapping(self):
        c,assets = self.resolved(source('90,0,90','-2'))
        self.assertTrue(assets,c.diagnostics)
        m = c.models[0].transform
        # Source anchor (0,0,-.5) goes to (0,0,-.508); +X becomes +Z.
        def apply(point):
            return tuple(sum(float(m[4*i+j])*point[j] for j in range(3))+float(m[4*i+3]) for i in range(3))
        for actual,wanted in zip(apply((0,0,-.5)),(0,0,-.508)):
            self.assertAlmostEqual(actual,wanted,places=6)
        for actual,wanted in zip(apply((1,0,-.5)),(0,0,.492)):
            self.assertAlmostEqual(actual,wanted,places=6)
        self.assertFalse(any(d.severity=='warning' for d in c.diagnostics))
        self.assertEqual(loads(dumps(c)),c)

    def test_far_source_origin_is_warned_without_correction(self):
        data = source()
        c,assets = self.resolved(edit_attrs(data,c_origin='400,299.5'))
        self.assertTrue(assets)
        self.assertTrue(any(d.code=='model_far_from_footprint' for d in c.diagnostics))
        self.assertNotEqual(c.models[0].translation[:2],(D(0),D(0)))
        c,assets = self.resolved(source(z='-1000'))
        self.assertTrue(assets)
        self.assertTrue(any(d.code=='model_far_from_footprint' for d in c.diagnostics))
        self.assertEqual(c.models[0].placement.position[2],D('-254'))

    def test_uniform_scale_uses_minimum_ratio(self):
        data = edit_attrs(source(),c_width=str(D(4)/D('.254')),c_height=str(D(12)/D('.254')))
        c,assets = self.resolved(data)
        self.assertEqual(c.models[0].scale,(D(2),)*3)

    def test_ad_scaled_resource_is_deterministic_and_original_unchanged(self):
        import model_geometry
        first = scaled_step(self.step,2)
        with model_geometry._LOCK:
            model_geometry._CACHE.clear()
        second = scaled_step(self.step,2)
        self.assertEqual(first,second)
        self.assertNotEqual(first,self.step)
        self.assertIn(b'CONVERSION_BASED_UNIT',first)
        data = edit_attrs(source(),c_width=str(D(4)/D('.254')),c_height=str(D(8)/D('.254')))
        c,assets = self.resolved(data,ad=True)
        self.assertTrue(assets,c.diagnostics)
        self.assertEqual(c.models[0].scale,(D(1),)*3)
        self.assertEqual(c.model_assets[0].source_sha256,hashlib.sha256(self.step).hexdigest())

    def test_scaling_ignores_unit_tokens_inside_strings_and_comments(self):
        trap = b"#999999=DESCRIPTIVE_REPRESENTATION_ITEM('fake; #88=', 'it''s LENGTH_UNIT() SI_UNIT($,.METRE.)');\n"
        comment = b"/* #999998=(LENGTH_UNIT() CONTEXT_DEPENDENT_UNIT('fake')); */\n"
        source_data = self.step.replace(b'ENDSEC;\nEND-ISO', trap+comment+b'ENDSEC;\nEND-ISO')
        self.assertIn(trap,source_data)
        scaled = _scale_units(source_data,2)
        self.assertIn(trap,scaled)
        self.assertIn(comment,scaled)
        self.assertIn(b'CONVERSION_BASED_UNIT',scaled)
        self.assertEqual(_scale_units(source_data,2),scaled)

    def test_scaling_rejects_existing_conversion_units_even_when_outer_box_matches(self):
        scaled = _scale_units(self.step,2)
        with self.assertRaisesRegex(DownloadError,'非 SI'):
            _scale_units(scaled,2)
        # A conversion context for an interior assembly part can be invisible
        # in the final bounding box; reject the context, not just bad bounds.
        extra = b"#999990=(CONVERSION_BASED_UNIT('inch',#999991) LENGTH_UNIT() NAMED_UNIT(#999992));\n"
        mixed = self.step.replace(b'ENDSEC;\nEND-ISO',extra+b'ENDSEC;\nEND-ISO')
        with self.assertRaisesRegex(DownloadError,'非 SI'):
            _scale_units(mixed,2)

    def test_ir_rejects_dangling_assets_and_missing_capability(self):
        c,_ = self.resolved()
        for invalid in (replace(c,model_assets=()), replace(c,required_features=('local-mm-y-up',)),
                        replace(c,model_assets=(replace(c.model_assets[0],bounds=(D(0),)*6),))):
            with self.assertRaises(DownloadError):
                dumps(invalid)

    def test_cancellation_is_not_a_model_warning(self):
        with self.assertRaises(Cancelled):
            resolve_models(parse_component(source(),'C20197'),lambda _:None,lambda _:None,
                           lambda:(_ for _ in ()).throw(Cancelled()))

    def test_embedded_kicad_payload_can_be_independently_decompressed(self):
        c,assets = self.resolved(source('20,30,40','-1'))
        raw = footprint_definition(c,'Box',model_assets=assets).decode()
        self.assertIn('(version 20241229)',raw)
        compressed = re.search(r'\(data \|([^|]+)\|\)',raw).group(1)
        payload = zstandard.ZstdDecompressor().decompress(base64.b64decode(compressed))
        self.assertEqual(payload,self.step)
        self.assertIn(hashlib.sha256(payload).hexdigest(),raw)
        matrix = rotation_zxy((20,30,40))
        expected = tuple(-v for v in euler_xyz(matrix))
        got = tuple(map(float,re.search(r'\(rotate \(xyz ([^)]+)\)',raw).group(1).split()))
        for a,b in zip(got,expected):self.assertAlmostEqual(a,b,places=5)

    def test_external_kicad_model_is_inside_library_and_committed_with_footprint(self):
        c,assets = self.resolved()
        artifacts,errors = build_library([c],{'C20197':('KICAD_FOOTPRINT',)},'Parts',model_assets=assets,model_mode='external')
        self.assertFalse(errors)
        self.assertEqual({a.format for a in artifacts},{'KICAD_MODEL','KICAD_FOOTPRINT'})
        written = commit_library(self.root/'exports',artifacts,overwrite=True)
        self.assertEqual(len(written),2)
        model = next(Path(p) for a,p in written if a.format=='KICAD_MODEL')
        footprint = next(Path(p) for a,p in written if a.format=='KICAD_FOOTPRINT')
        self.assertEqual(model.read_bytes(),self.step)
        self.assertIn('LCSC3D.3dshapes/'+model.name,footprint.read_text('utf-8'))
        self.assertIn('(version 20240108)',footprint.read_text('utf-8'))
        self.assertEqual(model.parent.parent,footprint.parent)

    def test_kicad_dimensionless_scale_preserves_small_positive_values(self):
        data = edit_attrs(source(),c_width=str(D('2e-7')/D('.254')),c_height=str(D('4e-7')/D('.254')))
        c,assets = self.resolved(data)
        raw = footprint_definition(c,'Small',model_assets=assets).decode()
        got = tuple(map(D,re.search(r'\(scale \(xyz ([^)]+)\)',raw).group(1).split()))
        self.assertEqual(got,(D('1e-7'),)*3)

    @unittest.skipUnless(sys.platform=='win32','Native CFB writer')
    def test_ad_dedup_uses_content_and_pose_instead_of_compression_or_guid(self):
        from altium_models import _rewrite_ids, _model_table, _pose_key
        from library_merge import footprint_signature, Reader, patch_parameters, indexed_component
        from compound_storage import compound_file
        c,assets = self.resolved()
        raw = export_pcblib(c,c.part,model_assets=assets)
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            streams = {'/'.join(p):ole.openstream(p).read() for p in ole.listdir()}
        def signature(content):
            index = LibraryIndex(content,'PCBLIB')
            try:
                return footprint_signature(indexed_component(index,index.names[0]))
            finally:
                index.ole.close()
        original = signature(raw)
        self.assertIsNotNone(original)
        values,block,payload = _model_table(streams)[0]
        guid = '{00000000-0000-0000-0000-000000000001}'
        rewritten = _rewrite_ids(streams,{_pose_key(values):guid})
        rewritten['Library/Models/Data'] = patch_parameters(Reader(block).block()[1],{'ID':guid})
        rewritten['Library/Models/0'] = zlib.compress(zlib.decompress(payload),1)
        changed = compound_file(rewritten)
        self.assertEqual(original,signature(changed))
        same,skipped = append_library(raw,changed,'PCBLIB')
        self.assertEqual(same,raw)
        self.assertTrue(skipped)
        shifted,other = self.resolved(source('20,30,40'))
        shifted = replace(shifted,part='C999')
        added,_ = append_library(raw,export_pcblib(shifted,shifted.part,model_assets=other),'PCBLIB')
        index = LibraryIndex(added,'PCBLIB')
        try:
            self.assertEqual(len(index.names),2)
        finally:
            index.ole.close()

    def test_model_write_failure_rolls_back_previous_library(self):
        c,assets = self.resolved()
        args = ([c],{'C20197':('KICAD_FOOTPRINT',)},'Parts')
        old,_ = build_library(*args,model_assets=assets,model_mode='external')
        root = self.root/'exports'
        commit_library(root,old,overwrite=True)
        before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        new,_ = build_library(*args,model_assets=assets,model_mode='embedded')
        def fail():
            if not any(root.rglob('*.step')): raise Cancelled()
        with self.assertRaises(Cancelled):
            commit_library(root,new,fail,overwrite=True)
        self.assertEqual({p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()},before)

    def test_same_pads_with_different_model_placements_are_not_merged(self):
        first,assets = self.resolved()
        second,other = self.resolved(source('0,0,90'))
        second = replace(second,part='C999')
        artifacts,errors = build_library([first,second],{c.part:('KICAD_FOOTPRINT',) for c in (first,second)},
                                        'Parts',model_assets={**assets,**other})
        self.assertFalse(errors)
        self.assertEqual(sum(a.format=='KICAD_FOOTPRINT' for a in artifacts),2)

    def test_backend_gets_models_without_standalone_step_selection(self):
        result = backend.download_part('C20197',backend.Options(self.root/'out',('KICAD_FOOTPRINT',)),self.api())
        self.assertEqual(result.status,'成功',result.message)
        self.assertTrue(any(p.endswith('.kicad_mod') for p in result.files))
        self.assertFalse(any(p.endswith('.step') for p in result.files))
        self.assertIn('kicad-embed://',Path(result.files[0]).read_text('utf-8'))

    def test_disabled_binding_does_not_request_model_resources(self):
        api = self.api()
        api.get_step_3d_model = lambda _:self.fail('unexpected STEP request')
        api.get_raw_3d_model_obj = lambda _:self.fail('unexpected OBJ request')
        result = backend.download_part('C20197',backend.Options(self.root/'out',('KICAD_FOOTPRINT',),bind_3d_models=False),api)
        self.assertEqual(result.status,'成功',result.message)
        self.assertFalse(result.model_diagnostics)

    def test_no_model_is_success_and_invalid_placement_is_partial(self):
        data = source()
        doc = data['packageDetail']['dataStr']
        doc['shape'] = [s for s in doc['shape'] if not s.startswith('SVGNODE~')]
        doc['head'].pop('uuid_3d',None)
        options = backend.Options(self.root/'out',('KICAD_FOOTPRINT',))
        result = backend.download_part('C20197',options,self.api(data))
        self.assertEqual(result.status,'成功',result.message)
        self.assertIn('无关联',result.message)
        result = backend.download_part('C20197',options,self.api(edit_attrs(source(),c_origin='')))
        self.assertEqual(result.status,'部分完成',result.message)
        self.assertTrue(result.files)
        self.assertTrue(result.model_diagnostics)

    @unittest.skipUnless(sys.platform=='win32','Native CFB writer')
    def test_ad_embeds_real_resource_checksum_and_merge_append_preserve_every_resource(self):
        from altium_models import model_checksum
        from altium_inspect import Reader,parameters
        first,assets = self.resolved()
        second,other = self.resolved(source('20,30,40'))
        second = replace(second,part='C999')
        library = MergedLibrary('PCBLIB')
        library.add(first,first.part,lambda:None,model_assets=assets)
        library.add(second,second.part,lambda:None,model_assets=other)
        content = library.build([first.part,second.part],lambda:None)
        with olefile.OleFileIO(io.BytesIO(content)) as ole:
            self.assertEqual(struct.unpack('<I',ole.openstream('Library/Models/Header').read())[0],2)
            reader = Reader(ole.openstream('Library/Models/Data').read())
            for index in range(2):
                p = parameters(reader.block()[1])
                payload = zlib.decompress(ole.openstream('Library/Models/'+str(index)).read())
                self.assertEqual(payload,self.step)
                self.assertEqual(int(p['CHECKSUM'])&0xffffffff,model_checksum(self.step))
        original = export_pcblib(first,first.part,model_assets=assets)
        added = export_pcblib(second,second.part,model_assets=other)
        merged,_ = append_library(original,added,'PCBLIB')
        with olefile.OleFileIO(io.BytesIO(merged)) as ole:
            self.assertEqual(struct.unpack('<I',ole.openstream('Library/Models/Header').read())[0],2)
        repeated,skipped = append_library(merged,added,'PCBLIB')
        self.assertEqual(repeated,merged)
        self.assertTrue(skipped)


if __name__ == '__main__':
    unittest.main()
