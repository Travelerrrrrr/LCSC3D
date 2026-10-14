"""Source independence, historical schema compatibility and domain isolation."""
import copy
from dataclasses import replace
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.eda.eda_ir import Component, Extension, Pin, Pad, Via, Point, Symbol, SymbolUnit, Footprint, D, loads, dumps, require_domain
from lcsc3d.eda.easyeda_import import parse_component
from lcsc3d.core.errors import DownloadError, Cancelled
from lcsc3d.eda.kicad import build_library, FORMATS
from lcsc3d.eda.altium import export_schlib, export_pcblib
from altium_inspect import schematic, pcb
from lcsc3d.core.app_logging import close_logging

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(part='C2765186'):
    return json.loads((FIXTURES / (part + '.json')).read_text('utf-8'))


class IRTests(unittest.TestCase):
    def test_extreme_coordinate_exponents_raise_a_domain_error(self):
        from lcsc3d.eda.eda_ir import decimal
        for value in ('1e999999999', '-1e999999999', '1e-999999999'):
            with self.subTest(value=value), self.assertRaises(DownloadError):
                decimal(value)
        self.assertEqual(decimal('1e-12'), D('1e-12'))
        self.assertEqual(decimal('1e-120'), D('1e-120'))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, LOCALAPPDATA=folder.name)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)

    def test_real_samples_survive_wire_roundtrip_and_both_ad_targets(self):
        cases = json.loads(gzip.decompress((FIXTURES / 'official_ad_cases.json.gz').read_bytes()))['cases']
        for case in cases:
            with self.subTest(part=case['part']):
                first = parse_component(case['data'], case['part'])
                second = loads(dumps(first))
                self.assertEqual(first, second)
                expected = [('model3d','info','model_origin_normalized')] if case['part'] in ('C57113','C83790') else []
                self.assertEqual([(d.domain,d.severity,d.code) for d in second.diagnostics],expected)
                old_pins = schematic(export_schlib(case['data'], case['part']))[2]
                new_pins = schematic(export_schlib(second, case['part']))[2]
                self.assertEqual(old_pins, new_pins)
                self.assertEqual(pcb(export_pcblib(case['data'], case['part']))[1],
                                 pcb(export_pcblib(second, case['part']))[1])

    def test_historical_1_0_document_supplies_optional_defaults(self):
        component = loads((FIXTURES / 'ir_v1_0.json').read_text('utf-8'))
        self.assertEqual(component.schema_version, '1.0')
        self.assertFalse(component.has_footprint)
        self.assertEqual(component.symbol.reference, 'U?')
        self.assertEqual(component.symbol.units[0].items[0].number, 'A1')
        self.assertEqual(loads(dumps(component)), component)
        current=loads((FIXTURES/'ir_v1_1_via.json').read_text('utf-8'))
        self.assertEqual(current.schema_version,'1.1')
        self.assertIsInstance(current.footprint.items[1],Via)
        self.assertEqual(loads(dumps(current)),current)

    def test_real_thermal_vias_remain_distinct_and_keep_copper_drill_and_span(self):
        import struct
        from test_kicad import sexpr, children, child
        from lcsc3d.eda.eda_targets import KICAD
        component = loads(dumps(parse_component(fixture('C31966'), 'C31966')))
        self.assertEqual(component.schema_version, '1.4')
        vias = [v for v in component.footprint.items if isinstance(v, Via)]
        self.assertEqual(len(vias), 4)
        self.assertEqual(len([p for p in component.footprint.items if isinstance(p, Pad)]), 9)
        expected = {('0.500126','0.499872'),('-0.499872','0.499872'),
                    ('-0.499872','-0.500126'),('0.500126','-0.500126')}
        self.assertEqual({(str(v.center.x),str(v.center.y)) for v in vias}, expected)
        self.assertTrue(all(v.diameter == D('0.6096') and v.drill == D('0.3048') and v.net == '' for v in vias))
        self.assertTrue(all((v.start_layer,v.end_layer) == ('top_copper','bottom_copper') for v in vias))
        _,pads,other = pcb(export_pcblib(component,'C31966'))
        self.assertEqual(len(pads),9)
        records = [b for kind,b in other if kind == 3]
        self.assertEqual(len(records),4)
        self.assertEqual({struct.unpack_from('<iiiiBB',b,13) for b in records},
            {(196900,196800,240000,120000,1,32),(-196800,196800,240000,120000,1,32),
             (-196800,-196900,240000,120000,1,32),(196900,-196900,240000,120000,1,32)})
        artifacts,errors = build_library([component],{component.part:FORMATS},'Vias')
        self.assertFalse(errors)
        pads = children(sexpr(next(a.payload for a in artifacts if a.format == 'KICAD_FOOTPRINT')),'pad')
        self.assertEqual(len(pads),13)
        holes = [p for p in pads if children(p,'drill')]
        self.assertEqual(len(holes),4)
        self.assertTrue(all(p[1:4] == ['', 'thru_hole', 'circle'] for p in holes))
        self.assertTrue(all(child(p,'size')[1:] == ['0.6096','0.6096'] and child(p,'drill')[1:] == ['0.3048'] for p in holes))
        limited = replace(KICAD,features=KICAD.features-{'through-vias'})
        with self.assertRaisesRegex(DownloadError,'through-vias'):
            limited.require(component,'footprint')
        self.assertIsNotNone(limited.require(component,'symbol'))

    def test_invalid_vias_and_future_spans_fail_without_discarding_other_domain(self):
        for value in ('0','-1','5'):
            data=fixture('C31966')
            shapes=data['packageDetail']['dataStr']['shape']
            index=next(i for i,s in enumerate(shapes) if s.startswith('VIA~'))
            fields=shapes[index].split('~');fields[5]=value;shapes[index]='~'.join(fields)
            component=loads(dumps(parse_component(data,'C31966')))
            self.assertIsNotNone(require_domain(component,'symbol'))
            with self.assertRaisesRegex(DownloadError,'孔径和铜环'):
                require_domain(component,'footprint')
        component=parse_component(fixture('C31966'),'C31966')
        altered=replace(component,footprint=replace(component.footprint,items=tuple(
            replace(i,end_layer='inner_copper_1') if isinstance(i,Via) else i for i in component.footprint.items)))
        with self.assertRaisesRegex(DownloadError,'贯穿'):
            export_pcblib(altered,'C31966')
        _,errors=build_library([altered],{'C31966':FORMATS},'Vias')
        self.assertIn(('C31966','KICAD_FOOTPRINT'),errors)

    def test_unknown_primitive_diagnostic_names_source_kind_in_both_targets(self):
        data=fixture('C31966')
        data['packageDetail']['dataStr']['shape'].append('FUTURE~PRIVATE-source-record')
        component=loads(dumps(parse_component(data,'C31966')))
        with self.assertRaisesRegex(DownloadError,'封装图元：FUTURE'):
            export_pcblib(component,'C31966')
        _,errors=build_library([component],{'C31966':FORMATS},'Vias')
        self.assertIn('封装图元：FUTURE',errors['C31966','KICAD_FOOTPRINT'])

    def test_pin_electrical_semantics_and_unknown_source_values_survive_roundtrip(self):
        for source, expected in (('', 'unspecified'), ('0', 'unspecified'), ('1', 'input'),
                ('2', 'output'), ('3', 'bidirectional'), ('4', 'power'), ('future', 'source_type_future')):
            data = fixture('C20197')
            shapes = data['dataStr']['shape']
            index = next(i for i, shape in enumerate(shapes) if shape.startswith('P~'))
            parts = shapes[index].split('~')
            parts[2] = source
            shapes[index] = '~'.join(parts)
            value = loads(dumps(parse_component(data, 'C20197')))
            pin = next(i for u in value.symbol.units for i in u.items if isinstance(i, Pin))
            self.assertEqual(pin.electrical, expected)
            self.assertTrue(any(json.loads(e.payload).get('electrical') == source for e in pin.extensions))
        wire = json.loads(dumps(value))
        item = next(i for i in wire['symbol']['units'][0]['items'] if i['@type'] == 'Pin')
        item.pop('electrical')
        legacy = loads(json.dumps(wire))
        self.assertEqual(next(i for i in legacy.symbol.units[0].items if isinstance(i, Pin)).electrical, 'unspecified')

    def test_real_reversed_paths_keep_connection_outside_body_in_ad_and_kicad(self):
        from test_kicad import sexpr, children, child
        component = loads(dumps(parse_component(fixture('C5879483'), 'C5879483')))
        pins = {i.number: i for u in component.symbol.units for i in u.items if isinstance(i, Pin)}
        self.assertEqual((pins['1'].connection.x, pins['1'].body.x), (D('-5.08'), D('-2.54')))
        self.assertEqual((pins['2'].connection.x, pins['2'].body.x), (D('5.08'), D('2.54')))
        for style, length in (('jlc', 10), ('ad', 20)):
            actual = {p['number']: p for p in schematic(export_schlib(component, component.part, symbol_style=style))[2]}
            for number, body, orientation in (('1', -10, 2), ('2', 10, 0)):
                self.assertEqual((actual[number]['x'], actual[number]['y'], actual[number]['length'], actual[number]['flags'] & 3),
                                 (body, 0, length, orientation))
        artifacts, errors = build_library([component], {component.part: ('KICAD_SYMBOL',)}, 'Resistor')
        self.assertFalse(errors)
        actual = {child(p, 'number')[1]: p for p in children(child(child(sexpr(artifacts[0].payload), 'symbol'), 'symbol'), 'pin')}
        self.assertEqual(child(actual['1'], 'at')[1:], ['-5.08', '0', '0'])
        self.assertEqual(child(actual['2'], 'at')[1:], ['5.08', '0', '180'])

    def test_pin_dot_selects_either_path_endpoint_in_all_four_directions(self):
        from lcsc3d.eda.kicad import symbol_definition
        from test_kicad import sexpr, child
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for reverse in (False, True):
                data = fixture('C5879483')
                index = next(i for i, s in enumerate(data['dataStr']['shape']) if s.startswith('P~'))
                groups = [g.split('~') for g in data['dataStr']['shape'][index].split('^^')]
                x, y = 400 + dx * 10, 300 + dy * 10
                groups[0][4:6] = ['999', '888']  # Legacy origin can differ from the explicit pin dot.
                groups[1] = [str(x), str(y)]
                groups[2][0] = f'M 400 300 L {x} {y}' if reverse else f'M {x} {y} L 400 300'
                data['dataStr']['shape'] = ['^^'.join('~'.join(g) for g in groups)]
                component = parse_component(data, 'C5879483')
                pin = require_domain(component, 'symbol').units[0].items[0]
                self.assertEqual(pin.connection, Point(D(dx) * D('2.54'), D(-dy) * D('2.54')))
                self.assertEqual(pin.body, Point())
                actual = schematic(export_schlib(component, component.part, symbol_style='ad'))[2][0]
                self.assertEqual((actual['x'], actual['y'], actual['length']), (0, 0, 20))
                self.assertEqual(actual['flags'] & 3, {(1, 0): 0, (-1, 0): 2, (0, 1): 3, (0, -1): 1}[dx, dy])
                kpin = child(child(sexpr(symbol_definition(component, 'Direction')), 'symbol'), 'pin')
                self.assertEqual(int(child(kpin, 'at')[3]), {(1, 0): 180, (-1, 0): 0, (0, 1): 90, (0, -1): 270}[dx, dy])
        groups[1] = ['405', '305']
        data['dataStr']['shape'] = ['^^'.join('~'.join(g) for g in groups)]
        component = parse_component(data, 'C5879483')
        with self.assertRaisesRegex(DownloadError, '连接点与线段端点不一致'):
            require_domain(component, 'symbol')

    def test_future_optional_fields_and_namespaced_extensions_are_retained(self):
        wire = json.loads(dumps(parse_component(fixture(), 'C2765186')))
        wire['schema_version'] = '1.7'
        wire['future_note'] = {'anything': [1, 2, '值']}
        wire['symbol']['future_symbol_attribute'] = {'@type': 'NotYetKnown'}
        value = loads(json.dumps(wire))
        rewritten = json.loads(dumps(value))
        self.assertEqual(rewritten['future_note'], wire['future_note'])
        self.assertEqual(rewritten['symbol']['future_symbol_attribute'], wire['symbol']['future_symbol_attribute'])
        value = replace(value, extensions=(Extension('example.optional', '2', '{"x":1}'),))
        self.assertEqual(loads(dumps(value)).extensions, value.extensions)
        self.assertIsNotNone(require_domain(value, 'symbol'))

    def test_new_major_and_required_features_never_silently_downgrade(self):
        component = parse_component(fixture(), 'C2765186')
        for modified in (replace(component, schema_version='2.0'),
                         replace(component, required_features=('local-mm-y-up', 'blind-vias'))):
            with self.assertRaises(DownloadError):
                require_domain(modified, 'symbol')
        unit = component.symbol.units[0]
        item = replace(unit.items[0], extensions=(Extension('example.critical', '1', required=True),))
        component = replace(component, symbol=replace(component.symbol,
                            units=(replace(unit, items=(item,) + unit.items[1:]),)))
        with self.assertRaisesRegex(DownloadError, '必要扩展'):
            require_domain(component, 'symbol')

    def test_malformed_wire_types_nonfinite_and_unknown_objects_fail(self):
        payloads = ['[]', '{"@type":"Component","part":3}', '{"@type":"NewComponent"}',
                    '{"@type":"Point","x":{"@decimal":"NaN"}}',
                    '{"@type":"Component","schema_version":"1.bad"}']
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(DownloadError):
                loads(payload)

    def test_bad_symbol_does_not_block_footprint_and_vice_versa(self):
        data = fixture()
        data['dataStr']['shape'] = []
        component = parse_component(data, 'C2765186')
        self.assertIsNotNone(require_domain(component, 'footprint'))
        with self.assertRaises(DownloadError):
            require_domain(component, 'symbol')
        artifacts, errors = build_library([component], {component.part: FORMATS}, 'Test')
        self.assertEqual({a.format for a in artifacts}, {'KICAD_FOOTPRINT'})
        self.assertIn((component.part, 'KICAD_SYMBOL'), errors)

    def test_domain_values_keep_pin_identity_copper_and_holes_separate(self):
        component = parse_component(fixture(), 'C2765186')
        pins = [i for u in component.symbol.units for i in u.items if isinstance(i, Pin)]
        pads = [i for i in component.footprint.items if isinstance(i, Pad)]
        self.assertEqual(len(pins), 14)
        self.assertEqual(len(pads), 18)
        self.assertEqual(pins[0].number, 'A1B12')
        self.assertEqual(pins[0].body.x, D('-6.350'))
        self.assertTrue(any(p.copper_center != p.center for p in pads))
        self.assertEqual(sum(p.mechanical_hole for p in pads), 2)
        self.assertEqual(sum(bool(p.slot_length) for p in pads), 4)

    def test_ir_is_not_mutated_by_styles_or_exporter_order(self):
        component = parse_component(fixture('C20197'), 'C20197')
        before = dumps(component)
        first = build_library([component], {component.part: FORMATS}, 'Test')
        export_schlib(component, component.part, symbol_style='ad')
        export_schlib(component, component.part, symbol_style='jlc')
        export_pcblib(component, component.part)
        self.assertEqual(before, dumps(component))
        self.assertEqual(first, build_library([component], {component.part: FORMATS}, 'Test'))

    def test_unknown_source_primitive_is_preserved_and_rejected_by_target(self):
        data = fixture()
        data['dataStr']['shape'].append('FUTURE~unknown_geometry~not_safe_to_drop')
        component = parse_component(data, 'C2765186')
        unknown = component.symbol.units[0].items[-1]
        self.assertIn('unknown_geometry', unknown.extensions[0].payload)
        self.assertEqual(loads(dumps(component)), component)
        with self.assertRaises(DownloadError):
            export_schlib(component, component.part)
        artifacts, errors = build_library([component], {component.part: FORMATS}, 'Test')
        self.assertIn((component.part, 'KICAD_SYMBOL'), errors)
        self.assertTrue(any(a.format == 'KICAD_FOOTPRINT' for a in artifacts))

    def test_parser_propagates_cancellation(self):
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            parse_component(fixture(), 'C2765186', cancel)

    def test_unmodelled_source_fields_are_preserved_without_becoming_a_target_dependency(self):
        component = parse_component(fixture('C20197'), 'C20197')
        first = component.symbol.units[0].items[0]
        source = next(e for e in first.extensions if e.namespace.endswith('.source-record'))
        self.assertIn('record', json.loads(source.payload))
        # The target still works after provenance is stripped from all items.
        stripped = replace(component, symbol=replace(component.symbol, units=tuple(
            replace(u, items=tuple(replace(i, extensions=()) for i in u.items)) for u in component.symbol.units)),
            footprint=replace(component.footprint, items=tuple(replace(i, extensions=()) for i in component.footprint.items)))
        self.assertEqual(build_library([component], {component.part: FORMATS}, 'Test'),
                         build_library([stripped], {component.part: FORMATS}, 'Test'))

    def test_manually_created_ir_needs_no_easyeda_metadata_for_linked_ad_or_kicad(self):
        component = Component(part='C1', title='Generic',
                              symbol=Symbol(name='Generic', units=(SymbolUnit(items=(
                                  Pin(number='A1', connection=Point(D('-2.54'), D(0))),)),)),
                              footprint=Footprint(name='GenericFootprint', items=(
                                  Pad(number='A1', size=Point(D(1), D(2))),)))
        _, records, _ = schematic(export_schlib(component, 'C1'))
        footprint = pcb(export_pcblib(component, 'C1'))[0]
        self.assertEqual(next(r['MODELNAME'] for r in records if r.get('RECORD') == '45'), footprint)
        artifacts, errors = build_library([component], {component.part: FORMATS}, 'GenericLib')
        self.assertFalse(errors)
        self.assertEqual(len(artifacts), 2)

    def test_limited_future_target_cannot_implicitly_accept_complex_core_features(self):
        from lcsc3d.eda.eda_targets import KICAD
        limited = replace(KICAD, name='FutureSimpleEDA', features=frozenset({'local-mm-y-up'}))
        with self.assertRaisesRegex(DownloadError, 'symbol-units'):
            limited.require(parse_component(fixture('C23922'), 'C23922'), 'symbol')
        with self.assertRaisesRegex(DownloadError, 'pad-copper-drill'):
            limited.require(parse_component(fixture(), 'C2765186'), 'footprint')
