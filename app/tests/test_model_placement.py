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
from unittest.mock import patch, Mock
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lcsc3d.services import backend
from lcsc3d.core.app_logging import close_logging
from lcsc3d.core.app_paths import temporary_directory
from lcsc3d.eda.easyeda_import import parse_component, model_outline_center
from lcsc3d.eda.eda_ir import Model3D, ModelPlacement, ModelAsset, dumps, loads
from lcsc3d.core.errors import DownloadError, Cancelled
from lcsc3d.models.model_export import resolve_models
from lcsc3d.models.model_geometry import rotation_zxy, euler_xyz, obj_origin, ModelOrigin
from lcsc3d.eda.altium import export_pcblib, MergedLibrary
from lcsc3d.eda.library_merge import LibraryIndex, append_library
from lcsc3d.eda.kicad import build_library, footprint_definition
from lcsc3d.eda.library_files import commit_library
import olefile
import zstandard

FIXTURES = Path(__file__).parent/'fixtures'
OBJ = '''v -1 -2 -.5
v 1 -2 -.5
v 1 2 -.5
v -1 2 -.5
v -1 -2 2.5
v 1 -2 2.5
v 1 2 2.5
v -1 2 2.5
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
        cls.step = (FIXTURES/'model_offset_box.step').read_bytes()

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
        return resolve_models(c,lambda ref:backend.StepPayload(self.step,identifier=ref.uuid),
                              prepare_altium=ad,origin_fetch=lambda ref:obj_origin(OBJ))

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
        for name in ('ir_v1_0.json','ir_v1_1_via.json','ir_v1_2_model.json'):
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

    def test_missing_position_and_nonfinite_vectors_never_default_to_zero(self):
        for updates in ({'c_origin':'nan,0'},{'c_rotation':'0,inf,0'},{'z':''}):
            with self.subTest(updates=updates):
                c = parse_component(edit_attrs(source(),**updates),'C20197')
                self.assertIsNone(c.models[0].placement)
                self.assertTrue(c.diagnostics)

    def test_source_dimensions_are_not_required_for_direct_placement(self):
        for updates in ({'c_width':''},{'c_width':'0','c_height':'nan'}):
            c,assets = self.resolved(edit_attrs(source(),**updates))
            self.assertTrue(assets,c.diagnostics)
            self.assertEqual(c.models[0].placement.size,())
            self.assertEqual(c.models[0].placement.convention,'source-anchor-zxy')

    def test_source_anchor_is_used_without_cad_or_subprocess(self):
        with patch('subprocess.Popen',side_effect=AssertionError('CAD process must not run')):
            c,assets = self.resolved(source(z='-2'),ad=True)
            self.assertEqual(list(assets.values()),[self.step])
            self.assertEqual(c.models[0].translation,(D(0),D(0),D('-.008')))
            self.assertEqual(c.model_assets[0].bounds,())
            self.assertFalse((self.root/'profile/LCSC3D/cache/step-bounds-v1').exists())

    def test_typec_anchor_regressions_have_independent_expected_ad_offsets(self):
        from altium_inspect import pcb, Reader, parameters
        # Public source extrema and JSON local poses, measured before this fix.
        # C2765186: no rotation; C165948: 180 degrees, so Y compensation reverses.
        cases = [('C2765186','v -4.47 -3.665 -2.58\nv 4.47 3.915 1.58\n',
                  (0,'-1.1999976','-.8999982'),'0,0,0',(0,'-1.3249976','1.6800018')),
                 ('C165948','v -4.47 -5.3 -.85\nv 4.47 2.6 3.251\n',
                  (0,'-1.0999978','-.850011'),'0,0,180',(0,'-2.4499978','-.000011'))]
        for part,obj,position,angles,expected in cases:
            with self.subTest(part=part):
                c = parse_component(source(angles),part)
                m = replace(c.models[0],placement=replace(c.models[0].placement,position=tuple(map(D,position))))
                c,assets = resolve_models(replace(c,models=(m,)),lambda _:backend.StepPayload(self.step),
                    origin_fetch=lambda _:obj_origin(obj),prepare_altium=True)
                body = next(b for kind,b in pcb(export_pcblib(c,part,model_assets=assets))[2] if kind==12)
                fields = parameters(Reader(body[18:]).block()[1])
                for key,value in zip(('MODEL.2D.X','MODEL.2D.Y','MODEL.3D.DZ'),expected):
                    self.assertAlmostEqual(float(D(fields[key].removesuffix('mil'))*D('.0254')),
                                           float(value),delta=.00000254)

    def test_anchor_scan_ignores_faces_materials_normals_and_supports_vertex_syntax(self):
        raw = 'newmtl a\nKd 99 99 99\nvn 500 500 500\nvt 500 500\n  v\t-1e0 2 -3 # a\r\nv 5 8 9 1\nf invalid\n'
        result = obj_origin(raw)
        self.assertEqual(result.position,(2,5,-3))
        self.assertEqual(result.sha256,hashlib.sha256(raw.encode()).hexdigest())

    def test_missing_invalid_anchor_skips_model_instead_of_silent_zero_origin(self):
        for raw in (None,'','f 1 2 3','v 0 0 0\n','v nan 0 0\nv 1 1 1',
                    'v 0 0\nv 1 1 1','v 1e20 0 0\nv 1 1 1'):
            with self.subTest(raw=raw):
                c,assets = resolve_models(parse_component(source(),'C20197'),lambda _:backend.StepPayload(self.step),
                                          origin_fetch=lambda _:obj_origin(raw))
                self.assertFalse(assets)
                self.assertIsNotNone(c.footprint)
                self.assertFalse(c.models[0].transform)
                self.assertTrue(any(d.severity=='error' and d.domain=='model3d' for d in c.diagnostics))

    def test_anchor_scan_cancellation_propagates(self):
        with self.assertRaises(Cancelled):
            obj_origin(OBJ,lambda:(_ for _ in ()).throw(Cancelled()))

    def test_anchor_cache_reuses_only_small_summary_and_obeys_disabled_cache(self):
        cache = backend.PayloadCache()
        api = self.api()
        api.use_cache = True
        api.get_raw_3d_model_obj = Mock(return_value=OBJ)
        with patch('lcsc3d.services.backend.PAYLOAD_CACHE',cache):
            first = api.get_model_origin('test-model')
            self.assertEqual(api.get_model_origin('test-model'),first)
            api.get_raw_3d_model_obj.assert_called_once()
            self.assertLess(cache.size,256)
            api.use_cache = False
            self.assertEqual(api.get_model_origin('test-model'),first)
            self.assertEqual(api.get_raw_3d_model_obj.call_count,2)

    def test_legacy_direct_origin_ir_keeps_its_original_transform(self):
        c = parse_component(source(),'C20197')
        m = replace(c.models[0],placement=replace(c.models[0].placement,convention='step-origin-zxy'))
        c,assets = resolve_models(replace(c,models=(m,)),lambda _:backend.StepPayload(self.step))
        c = replace(c,schema_version='1.3')
        self.assertEqual(c.models[0].translation,(D(0),)*3)
        self.assertEqual(loads(dumps(c)),c)

    def test_corrupt_step_skips_only_model(self):
        c = parse_component(source(),'C20197')
        c,assets = resolve_models(c,lambda _:backend.StepPayload(b'ISO-10303-21; broken'))
        self.assertFalse(assets)
        self.assertFalse(c.models[0].transform)
        self.assertIsNotNone(c.footprint)
        self.assertTrue(any(d.severity=='error' and d.domain=='model3d' for d in c.diagnostics))

    def test_ad_unrepresentable_json_position_skips_only_the_model(self):
        c = parse_component(source(),'C20197')
        model = replace(c.models[0],placement=replace(c.models[0].placement,position=(D(6000),D(0),D(0))))
        c = replace(c,models=(model,))
        c,assets = resolve_models(c,lambda _:backend.StepPayload(self.step),prepare_altium=True,
                                 origin_fetch=lambda _:obj_origin(OBJ))
        self.assertFalse(assets)
        self.assertIsNotNone(c.footprint)
        self.assertFalse(c.models[0].transform)
        self.assertIn('AD 坐标范围',c.diagnostics[-1].message)

    def test_source_zxy_rotation_and_negative_z_have_expected_affine_mapping(self):
        c,assets = self.resolved(source('90,0,90','-2'))
        self.assertTrue(assets,c.diagnostics)
        m = c.models[0].transform
        # The source anchor goes to JSON Z; a point one mm along +X becomes +Z.
        def apply(point):
            return tuple(sum(float(m[4*i+j])*point[j] for j in range(3))+float(m[4*i+3]) for i in range(3))
        for actual,wanted in zip(apply((0,0,-.5)),(0,0,-.508)):
            self.assertAlmostEqual(actual,wanted,places=6)
        for actual,wanted in zip(apply((1,0,-.5)),(0,0,.492)):
            self.assertAlmostEqual(actual,wanted,places=6)
        self.assertFalse(any(d.severity=='warning' for d in c.diagnostics))
        self.assertEqual(loads(dumps(c)),c)

    def test_valid_source_offsets_and_negative_height_are_preserved_without_geometry_warnings(self):
        data = source()
        c,assets = self.resolved(edit_attrs(data,c_origin='400,299.5'))
        self.assertTrue(assets)
        self.assertFalse(any(d.severity in ('warning','error') for d in c.diagnostics))
        self.assertNotEqual(c.models[0].translation[:2],(D(0),D(0)))
        c,assets = self.resolved(source(z='-1000'))
        self.assertTrue(assets)
        self.assertFalse(any(d.severity in ('warning','error') for d in c.diagnostics))
        self.assertEqual(c.models[0].placement.position[2],D('-254'))

    def test_source_dimensions_never_rescale_original_step_or_placement(self):
        baseline,_ = self.resolved(source('20,30,40','-2'))
        for factor in (D('.999998'),D(2),D('1e-7')):
            data = edit_attrs(source('20,30,40','-2'),c_width=str(D(2)*factor/D('.254')),
                              c_height=str(D(4)*factor/D('.254')))
            for ad in (False, True):
                with self.subTest(factor=factor, ad=ad):
                    c,assets = self.resolved(data,ad=ad)
                    self.assertEqual(c.models[0].scale,(D(1),)*3)
                    self.assertEqual(c.models[0].transform,baseline.models[0].transform)
                    self.assertEqual(list(assets.values()),[self.step])
                    self.assertEqual(c.model_assets[0].sha256,hashlib.sha256(self.step).hexdigest())
                    self.assertEqual(c.model_assets[0].source_sha256,c.model_assets[0].sha256)
                    self.assertEqual(c.model_assets[0].reference_sha256,hashlib.sha256(OBJ.encode()).hexdigest())
                    if ad:
                        raw = export_pcblib(c,c.part,model_assets=assets)
                        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
                            self.assertEqual(zlib.decompress(ole.openstream('Library/Models/0').read()),self.step)

    def test_fallback_step_is_original_and_never_claims_obj_verification(self):
        c = parse_component(source(),'C20197')
        origin_fetch = Mock(return_value=obj_origin(OBJ))
        c,assets = resolve_models(c,lambda _:backend.StepPayload(self.step,used_library=True,identifier='fallback'),
                                 origin_fetch=origin_fetch)
        self.assertEqual(origin_fetch.call_args.args[0].uuid,'fallback')
        self.assertEqual(list(assets.values()),[self.step])
        self.assertEqual(c.model_assets[0].identifier,'fallback')
        message = next(d.message for d in c.diagnostics if d.code=='model_library_resolved')
        self.assertNotIn('核对',message)

    def test_step_units_are_preserved_for_target_eda_without_geometry_interpretation(self):
        self.assertIn(b'SI_UNIT(.MILLI.,.METRE.)',self.step)
        centimetres = self.step.replace(b'SI_UNIT(.MILLI.,.METRE.)',b'SI_UNIT(.CENTI.,.METRE.)')
        c,assets = resolve_models(parse_component(source(),'C20197'),
            lambda _:backend.StepPayload(centimetres),prepare_altium=True,
            origin_fetch=lambda _:ModelOrigin((0,0,0),''))
        self.assertEqual(list(assets.values()),[centimetres],c.diagnostics)
        self.assertEqual(c.model_assets[0].bounds,())
        self.assertEqual(c.models[0].translation,(D(0),)*3)
        self.assertEqual(c.models[0].scale,(D(1),)*3)

    def test_ir_rejects_dangling_assets_and_missing_capability(self):
        c,_ = self.resolved()
        for invalid in (replace(c,model_assets=()), replace(c,required_features=('local-mm-y-up',)),
                        replace(c,required_features=tuple(f for f in c.required_features if f!='model-direct-placement')),
                        replace(c,required_features=tuple(f for f in c.required_features if f!='model-source-anchor')),
                        replace(c,model_assets=(replace(c.model_assets[0],bounds=(D(0),)*6),))):
            with self.assertRaises(DownloadError):
                dumps(invalid)

    def test_direct_placement_requires_explicit_target_support(self):
        from lcsc3d.eda.eda_targets import ALTIUM
        c,_ = self.resolved()
        self.assertEqual(c.schema_version,'1.4')
        target = replace(ALTIUM,features=ALTIUM.features-{'model-direct-placement'})
        with self.assertRaisesRegex(DownloadError,'model-direct-placement'):
            target.require(c,'footprint')
        with self.assertRaisesRegex(DownloadError,'model-source-anchor'):
            replace(ALTIUM,features=ALTIUM.features-{'model-source-anchor'}).require(c,'footprint')

    def test_ad_body_proxy_does_not_change_anchored_pose(self):
        from altium_inspect import pcb, Reader, parameters
        c,assets = self.resolved(source('20,30,40','-2'),ad=True)
        raw = export_pcblib(c,c.part,model_assets=assets)
        body = next(b for kind,b in pcb(raw)[2] if kind==12)
        params = parameters(Reader(body[18:]).block()[1])
        for index,key in enumerate(('MODEL.2D.X','MODEL.2D.Y','MODEL.3D.DZ')):
            actual=float(D(params[key].removesuffix('mil'))*D('.0254'))
            self.assertAlmostEqual(actual,float(c.models[0].translation[index]),delta=.00000254)
        self.assertEqual(params['STANDOFFHEIGHT'],'0.0000000000mil')
        self.assertEqual(params['OVERALLHEIGHT'],'0.0000000000mil')

    @unittest.skipUnless(sys.platform=='win32','Native CFB writer')
    def test_ad_model_layer_changes_only_the_body_layer(self):
        from altium_inspect import pcb, Reader, parameters
        c,assets = self.resolved(source('20,30,40','-2'),ad=True)
        baseline = export_pcblib(c,c.part,model_assets=assets)
        _,pads,primitives = pcb(baseline)
        original = next(body for kind,body in primitives if kind==12)
        original_reader = Reader(original[18:])
        original_values = parameters(original_reader.block()[1])
        for layer in range(1,33):
            with self.subTest(layer=layer):
                content = export_pcblib(c,c.part,model_assets=assets,model_layer=layer)
                _,actual_pads,actual_primitives = pcb(content)
                self.assertEqual(actual_pads,pads)
                self.assertEqual([p for p in actual_primitives if p[0]!=12],
                                 [p for p in primitives if p[0]!=12])
                body = next(body for kind,body in actual_primitives if kind==12)
                self.assertEqual(body[0],56+min(layer,16))
                self.assertEqual(body[1:18],original[1:18])
                reader = Reader(body[18:])
                values = parameters(reader.block()[1])
                self.assertEqual(values,{**original_values,'V7_LAYER':f'MECHANICAL{layer}'})
                self.assertEqual(body[18+reader.offset:],original[18+original_reader.offset:])
                with olefile.OleFileIO(io.BytesIO(content)) as ole:
                    self.assertEqual(zlib.decompress(ole.openstream('Library/Models/0').read()),self.step)

    @unittest.skipUnless(sys.platform=='win32','Native CFB writer')
    def test_backend_uses_ad_layer_for_individual_merge_append_and_copies(self):
        from altium_inspect import pcb, Reader, parameters
        for mode,layer in (('individual',13),('merge',17),('append',32)):
            with self.subTest(mode=mode):
                root = self.root/mode
                root.mkdir()
                options = backend.Options(root,('PCBLIB',),ad_3d_layer=layer,
                    merge_pcblib=mode=='merge',keep_pcblib=True)
                target = root/'Parts.PcbLib'
                if mode=='append':
                    c,assets = self.resolved(ad=True)
                    target.write_bytes(export_pcblib(c,c.part,model_assets=assets))
                    options = replace(options,append_mode=True,pcblib_target=str(target),keep_individual=True)
                results = backend.download_batch(['C999'],options,api=self.api(),workers=1)
                self.assertEqual([r.status for r in results],['成功'],results[0].message)
                paths = {Path(p) for p in results[0].files}
                self.assertEqual(len(paths),1 if mode=='individual' else 2)
                for path in paths:
                    layers = []
                    with olefile.OleFileIO(path) as ole:
                        sections = [s[0] for s in ole.listdir() if len(s)==2 and s[0]!='Library' and s[1]=='Data']
                    for section in sections:
                        for kind,body in pcb(path.read_bytes(),section)[2]:
                            if kind==12:
                                values = parameters(Reader(body[18:]).block()[1])
                                layers.append(values['V7_LAYER'])
                                number = int(values['V7_LAYER'].removeprefix('MECHANICAL'))
                                self.assertEqual(body[0],56+min(number,16))
                    expected = ['MECHANICAL1',f'MECHANICAL{layer}'] if mode=='append' and path==target else [f'MECHANICAL{layer}']
                    self.assertCountEqual(layers,expected)

    def test_invalid_ad_model_layers_are_rejected_and_diagnosable(self):
        from lcsc3d.core import app_logging as logs
        c,assets = self.resolved(ad=True)
        for layer in (0,33,-1,True,13.0,'13',None):
            with self.subTest(layer=layer):
                with self.assertRaises(DownloadError):
                    backend.Options(self.root,('PCBLIB',),ad_3d_layer=layer)
                with self.assertRaises(DownloadError):
                    export_pcblib(c,c.part,model_assets=assets,model_layer=layer)
        log_dir = self.root/'profile/LCSC3D/logs'
        logs.configure_logging(directory=log_dir)
        export_pcblib(c,c.part,model_assets=assets,model_layer=13)
        with self.assertRaises(DownloadError):
            export_pcblib(c,c.part,model_assets=assets,model_layer=33)
        rows = [json.loads(line) for line in (log_dir/'LCSC3D.log').read_text('utf-8').splitlines()]
        written = next(r for r in rows if r['event']=='export.ad_models_embedded' and r.get('ad_3d_layer')==13)
        self.assertEqual(written['count'],1)
        failed = next(r for r in reversed(rows) if r['event']=='export.pcblib.failed')
        self.assertEqual(failed['ad_3d_layer'],33)
        self.assertEqual(failed['error'],'DownloadError')
        self.assertTrue(failed['error_id'])
        self.assertTrue(failed['frames'])

    def test_cancellation_is_not_a_model_warning(self):
        with self.assertRaises(Cancelled):
            resolve_models(parse_component(source(),'C20197'),lambda _:None,
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

    def test_kicad_writes_unit_scale_even_for_tiny_source_dimensions(self):
        data = edit_attrs(source(),c_width=str(D('2e-7')/D('.254')),c_height=str(D('4e-7')/D('.254')))
        c,assets = self.resolved(data)
        raw = footprint_definition(c,'Small',model_assets=assets).decode()
        got = tuple(map(D,re.search(r'\(scale \(xyz ([^)]+)\)',raw).group(1).split()))
        self.assertEqual(got,(D(1),)*3)

    @unittest.skipUnless(sys.platform=='win32','Native CFB writer')
    def test_ad_dedup_uses_content_and_pose_instead_of_compression_or_guid(self):
        from lcsc3d.eda.altium_models import _rewrite_ids, _model_table, _pose_key
        from lcsc3d.eda.library_merge import footprint_signature, Reader, patch_parameters, indexed_component
        from lcsc3d.eda.compound_storage import compound_file
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

    def test_backend_binds_original_step_with_anchor_only_obj_read(self):
        for fmt in ('PCBLIB','KICAD_FOOTPRINT'):
            api = self.api()
            api.get_raw_3d_model_obj = Mock(return_value=OBJ)
            with patch('lcsc3d.models.model3d.read_obj',side_effect=AssertionError('mesh must not be built')):
                result = backend.download_part('C20197',backend.Options(self.root/fmt,(fmt,)),api)
            self.assertEqual(result.status,'成功',result.message)
            api.get_raw_3d_model_obj.assert_called_once_with('test-model')
            self.assertFalse(any(p.endswith('.step') for p in result.files))
            self.assertTrue(result.files)

    def test_explicit_obj_download_is_still_available_with_model_binding(self):
        api = self.api()
        api.get_raw_3d_model_obj = Mock(return_value=OBJ)
        result = backend.download_part('C20197',backend.Options(self.root/'out',('PCBLIB','OBJ')),api)
        self.assertEqual(result.status,'成功',result.message)
        api.get_raw_3d_model_obj.assert_called_once_with('test-model')
        self.assertEqual(next(Path(p) for p in result.files if p.endswith('.obj')).read_text('utf-8'),OBJ)

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
        from lcsc3d.eda.altium_models import model_checksum
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
