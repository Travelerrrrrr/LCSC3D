from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_schlib, export_pcblib
from backend import Options, download_part
from errors import Cancelled, DownloadError
from eda_geometry import path_points

if sys.platform == 'win32':
    from altium_inspect import schematic, pcb

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(part='C2765186'):
    return json.loads((FIXTURES / (part + '.json')).read_text('utf-8'))


class Api:
    def __init__(self, data):
        self.data = data
        self.check_cancelled = lambda: None

    def get_cad_data_of_component(self, part):
        return self.data


@unittest.skipUnless(sys.platform == 'win32', 'AD CFB export uses Windows structured storage')
class LibraryTests(unittest.TestCase):
    def test_type_c_keeps_all_pin_names_numbers_and_native_footprint_reference(self):
        header, records, pins = schematic(export_schlib(fixture(), 'C2765186'))
        self.assertEqual(header['COMPCOUNT'], '1')
        self.assertEqual(len(pins), 14)
        self.assertEqual([p['number'] for p in pins], ['A1B12', 'A4B9', 'B8', 'A5', 'B7', 'A6', 'A7', 'B6',
                                                   'A8', 'B5', 'B4A9', 'B1A12', '14', '13'])
        self.assertEqual([p['name'] for p in pins], ['GND', 'VBUS', 'SBU2', 'CC1', 'Dn2', 'Dp1', 'Dn1', 'Dp2',
                                                 'SBU1', 'CC2', 'VBUS', 'GND', 'EH', 'EH'])
        self.assertEqual((pins[0]['x'], pins[0]['y'], pins[0]['length']), (-25, 55, 10))
        self.assertTrue(all(p['electrical'] == 4 for p in pins))
        impl = next(r for r in records if r.get('RECORD') == '45')
        name, _, _ = pcb(export_pcblib(fixture(), 'C2765186'))
        self.assertEqual(impl['MODELNAME'], name)
        self.assertEqual(impl['MODELTYPE'], 'PCBLIB')
        self.assertEqual(records[int(impl['OWNERINDEX'])]['RECORD'], '44')
        params = {r.get('NAME'): r.get('TEXT') for r in records if r.get('RECORD') == '41'}
        self.assertEqual(params['SupplierPart'], 'C2765186')
        self.assertEqual(params['Manufacturer'], 'SHOU HAN(首韩)')
        self.assertEqual(params['JLCEDA'], 'https://lceda.cn/')
        self.assertEqual(params['EasyEDA'], 'https://easyeda.com/')

    def test_type_c_physical_pad_geometry_slots_and_nonplated_holes(self):
        _, pads, other = pcb(export_pcblib(fixture(), 'C2765186'))
        named = [p for p in pads if p['number']]
        holes = [p for p in pads if not p['number']]
        self.assertEqual(len(named), 16)
        self.assertEqual(Counter(p['number'] for p in named)['13'], 2)
        self.assertEqual(Counter(p['number'] for p in named)['14'], 2)
        self.assertEqual(Counter(p['layer'] for p in named), {1: 12, 74: 4})
        self.assertEqual((named[0]['x'], named[0]['y']), (-1259810, 935050))
        self.assertEqual((named[0]['width'], named[0]['height']), (216540, 433070))
        self.assertEqual((named[0]['paste'], named[0]['solder']), (0, 19690))
        self.assertEqual((named[0]['offset_x'], named[0]['offset_y']), (-40, 0))
        slots = named[-4:]
        self.assertEqual([p['slot'] for p in slots], [511810, 511810, 393700, 393700])
        self.assertTrue(all(p['hole_type'] == 2 and p['drill'] == 236220 and p['plated'] for p in slots))
        self.assertTrue(all(abs(p['hole_rotation'] % 180 - 90) < 1e-7 for p in slots))
        self.assertEqual(len(holes), 2)
        self.assertTrue(all(not p['plated'] and p['drill'] == 275600 for p in holes))
        self.assertEqual(Counter(kind for kind, _ in other), {1: 2, 4: 5, 11: 18})

    def test_qfn_and_resistor_array_also_keep_all_electrical_primitives(self):
        for part, count in [('C2040', 57), ('C20197', 8), ('C163691', 2)]:
            with self.subTest(part=part):
                _, _, pins = schematic(export_schlib(fixture(part), part))
                _, pads, _ = pcb(export_pcblib(fixture(part), part))
                self.assertEqual(len(pins), count)
                self.assertEqual(Counter(p['number'] for p in pins), Counter(p['number'] for p in pads))

    def test_fractional_and_unicode_pin_fields_survive_serialization(self):
        data = fixture()
        data['dataStr']['head']['x'] += .125
        _, _, pins = schematic(export_schlib(data, 'C2765186'))
        self.assertEqual(pins[0]['x'], -25.125)
        data['dataStr']['shape'][1] = data['dataStr']['shape'][1].replace('~GND~', '~接地~')
        _, _, pins = schematic(export_schlib(data, 'C2765186'))
        self.assertEqual(pins[0]['name'], '接地')
        self.assertEqual(pins[0]['x'], -25.125)

    def test_unicode_pin_keeps_independent_text_styles_and_electrical_symbols(self):
        data = fixture()
        data['dataStr']['head']['x'] += .125
        groups = [g.split('~') for g in data['dataStr']['shape'][1].split('^^')]
        groups[3][4], groups[3][6:9] = '接地', ['Tahoma', '5.8pt', '#00FF00']
        groups[4][6:9] = ['Arial', '9pt', '#FF0000']
        groups[5][0] = groups[6][0] = '1'
        data['dataStr']['shape'][1] = '^^'.join('~'.join(g) for g in groups)
        header, records, pins = schematic(export_schlib(data, 'C2765186'))
        row = next(r for r in records if r.get('RECORD') == '2')
        self.assertEqual((pins[0]['name'], pins[0]['x'], pins[0]['inner'], pins[0]['outer']),
                         ('接地', -25.125, 3, 1))
        name_font, number_font = row['NAME_CUSTOMFONTID'], row['DESIGNATOR_CUSTOMFONTID']
        self.assertEqual((header['FONTNAME' + name_font], header['SIZE' + name_font]), ('Tahoma', '6'))
        self.assertEqual((header['FONTNAME' + number_font], header['SIZE' + number_font]), ('Arial', '9'))
        self.assertEqual(int(row['NAME_CUSTOMCOLOR']), 0x00ff00)
        self.assertEqual(int(row['DESIGNATOR_CUSTOMCOLOR']), 0x0000ff)

    def test_resistor_array_polylines_match_official_ad_export_and_meet_pins(self):
        official = json.loads((FIXTURES / 'C20197_ad_geometry.json').read_text('utf-8'))
        _, records, pins = schematic(export_schlib(fixture('C20197'), 'C20197'))
        lines = [r for r in records if r.get('RECORD') == '6']
        self.assertEqual(len(lines), len(official['polylines']))
        vertices = []
        for record, baseline in zip(lines, official['polylines']):
            actual = [[float(record[f'X{i}']) + float(record.get(f'X{i}_FRAC', 0)) / 100000,
                       float(record[f'Y{i}']) + float(record.get(f'Y{i}_FRAC', 0)) / 100000]
                      for i in range(1, int(record['LOCATIONCOUNT']) + 1)]
            self.assertEqual(actual, baseline['vertices'])
            vertices.extend(actual)
        # Pin body endpoints must touch their resistor/body line geometry.
        self.assertTrue(all([p['x'], p['y']] in vertices for p in pins))
        self.assertEqual([min(p[0] for p in vertices), max(p[0] for p in vertices),
                          min(p[1] for p in vertices), max(p[1] for p in vertices)], [-15, 15, -25, 25])

    def test_polygon_and_curve_coordinates_use_dxp_units_and_keep_fractions(self):
        data = fixture('C20197')
        data['dataStr']['shape'].append('PG~0.125 0.25 5.5 0.25 5.5 5.75~#880000~1~0~#FFFFFF~test~0')
        head = data['dataStr']['head']
        head['x'], head['y'] = 0, 0
        _, records, _ = schematic(export_schlib(data, 'C20197'))
        polygon = next(r for r in records if r.get('RECORD') == '7')
        self.assertEqual((polygon['X1'], polygon['X1_FRAC']), ('0', '12500'))
        self.assertEqual((polygon['Y1'], polygon['Y1_FRAC']), ('0', '-25000'))
        self.assertEqual((polygon['X2'], polygon['X2_FRAC']), ('5', '50000'))
        _, records, pins = schematic(export_schlib(fixture('C163691'), 'C163691'))
        line_vertices = [(float(r[f'X{i}']) + float(r.get(f'X{i}_FRAC', 0)) / 100000,
                          float(r[f'Y{i}']) + float(r.get(f'Y{i}_FRAC', 0)) / 100000)
                         for r in records if r.get('RECORD') == '6'
                         for i in range(1, int(r['LOCATIONCOUNT']) + 1)]
        self.assertTrue(line_vertices)
        self.assertLess(max(abs(x) for x, _ in line_vertices), 50)
        self.assertLess(max(abs(y) for _, y in line_vertices), 50)

    def test_long_footprint_names_keep_reference_and_discovery_keys(self):
        data = fixture()
        data['packageDetail']['title'] += '_EXTENDED_LIBRARY_NAME'
        header, records, _ = schematic(export_schlib(data, 'C2765186'))
        name, _, _ = pcb(export_pcblib(data, 'C2765186'))
        self.assertEqual(name, data['packageDetail']['title'])
        self.assertEqual(next(r for r in records if r.get('RECORD') == '45')['MODELNAME'], name)

    def test_unfilled_pcb_rectangle_keeps_its_four_edges_and_line_width(self):
        data = fixture('C20197')
        doc = data['packageDetail']['dataStr']
        doc['head']['x'], doc['head']['y'] = 4000, 3000
        _, _, original = pcb(export_pcblib(data, 'C20197'))
        # Actual C5593 official rectangle, found during the ten-part check.
        rectangle = 'RECT~3982.75~2994.5~34.5~11~3~gge1824~0~0.7874~none~~~'
        doc['shape'].append(rectangle)
        _, _, primitives = pcb(export_pcblib(data, 'C20197'))
        self.assertEqual(len(primitives), len(original) + 4)
        outline = primitives[-4:]
        expected = [(-1725000, 550000, 1725000, 550000),
                    (1725000, 550000, 1725000, -550000),
                    (1725000, -550000, -1725000, -550000),
                    (-1725000, -550000, -1725000, 550000)]
        self.assertEqual([kind for kind, _ in outline], [4] * 4)
        for (_, body), edge in zip(outline, expected):
            self.assertEqual(body[0], 33)  # Top overlay, not copper.
            self.assertEqual(struct.unpack_from('<iiiii', body, 13), (*edge, 78740))
        doc['shape'][-1] = rectangle.replace('~0.7874~none~', '~0~solid~')
        _, _, primitives = pcb(export_pcblib(data, 'C20197'))
        self.assertEqual(primitives[-1][0], 11)
        self.assertEqual(len(primitives), len(original) + 1)

    def test_schematic_rectangles_with_no_radius_use_the_native_rectangle(self):
        _, records, _ = schematic(export_schlib(fixture('C2040'), 'C2040'))
        rectangle = next(r for r in records if r.get('RECORD') == '14')
        self.assertEqual((rectangle['LOCATION.X'], rectangle['LOCATION.Y'],
                          rectangle['CORNER.X'], rectangle['CORNER.Y']), ('-115', '-180', '115', '200'))
        self.assertFalse(any(r.get('RECORD') == '10' for r in records))
        _, records, _ = schematic(export_schlib(fixture(), 'C2765186'))
        rounded = next(r for r in records if r.get('RECORD') == '10')
        self.assertEqual((rounded['CORNERXRADIUS'], rounded['CORNERYRADIUS']), ('2', '2'))

    def test_ad_only_export_works_without_a_3d_model_and_replaces_existing_libraries(self):
        data = fixture()
        data['packageDetail']['dataStr']['shape'] = [s for s in data['packageDetail']['dataStr']['shape'] if not s.startswith('SVGNODE~')]
        data['packageDetail']['dataStr']['head'].pop('uuid_3d', None)
        with tempfile.TemporaryDirectory() as folder:
            options = Options(Path(folder), ('SCHLIB', 'PCBLIB'))
            result = download_part('C2765186', options, Api(data))
            self.assertEqual(result.status, '成功', result.message)
            self.assertEqual({Path(f).suffix for f in result.files}, {'.SchLib', '.PcbLib'})
            self.assertEqual({Path(f).name for f in result.files},
                             {data['title'] + '.SchLib', data['title'] + '.PcbLib'})
            for file in result.files:
                Path(file).write_bytes(b'old library')
            with patch('backend.export_schlib', wraps=export_schlib) as schlib, \
                 patch('backend.export_pcblib', wraps=export_pcblib) as pcblib:
                refreshed = download_part('C2765186', options, Api(data))
            self.assertEqual(refreshed.status, '成功', refreshed.message)
            self.assertEqual(refreshed.files, result.files)
            schlib.assert_called_once()
            pcblib.assert_called_once()
            self.assertEqual(len(schematic(Path(refreshed.files[0]).read_bytes())[2]), 14)
            self.assertEqual(len(pcb(Path(refreshed.files[1]).read_bytes())[1]), 18)

    def test_missing_model_does_not_block_libraries_in_mixed_selection(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as folder, patch('backend.model_reference', return_value=None):
            result = download_part('C2765186', Options(Path(folder), ('STEP', 'SCHLIB', 'PCBLIB')), Api(data))
            self.assertEqual(result.status, '部分完成')
            self.assertEqual(len(result.files), 2)
            self.assertIn('STEP', result.message)

    def test_unsupported_geometry_never_commits_a_partial_library(self):
        for field, export in [('dataStr', export_schlib), ('packageDetail', export_pcblib)]:
            data = fixture()
            doc = data[field] if field == 'dataStr' else data[field]['dataStr']
            doc['shape'].append('UNSUPPORTED~geometry')
            with self.assertRaisesRegex(DownloadError, '不支持'):
                export(data, 'C2765186')
        data = fixture()
        data['packageDetail']['dataStr']['shape'].append('UNSUPPORTED~geometry')
        with tempfile.TemporaryDirectory() as folder:
            result = download_part('C2765186', Options(Path(folder), ('SCHLIB', 'PCBLIB')), Api(data))
            self.assertEqual(result.status, '部分完成')
            self.assertEqual(len(result.files), 1)
            self.assertFalse(list(Path(folder).rglob('*.PcbLib')))

    def test_cancellation_and_parallel_com_apartments(self):
        def stop(): raise Cancelled()
        with self.assertRaises(Cancelled):
            export_pcblib(fixture(), 'C2765186', stop)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: pcb(export_pcblib(fixture(), 'C2765186'))[1], range(6)))
        self.assertEqual([len(p) for p in results], [18] * 6)


class GeometryTests(unittest.TestCase):
    def test_curves_keep_endpoints_closed_contours_and_multiple_subpaths(self):
        paths = path_points('M0 0 h10 v5 H0 Z M20 0 Q25 10 30 0 T40 0')
        self.assertEqual(paths[0], [(0, 0), (10, 0), (10, 5), (0, 5), (0, 0)])
        self.assertEqual(paths[1][0], (20, 0))
        self.assertEqual(paths[1][-1], (40, 0))
        self.assertGreater(len(paths[1]), 10)
        arc = path_points('M0 0 A5 5 0 0 1 10 0')[0]
        self.assertEqual(arc[-1], (10, 0))
        self.assertGreater(len(arc), 20)

    def test_invalid_paths_are_rejected(self):
        for path in ('L 1 2', 'M 1', 'M 0 0 A5 5 0 2 0 3 4', 'M0 0 X1 2', 'M0 0 nan 2'):
            with self.subTest(path=path), self.assertRaises(DownloadError):
                path_points(path)
