"""Semantic checks against 27 independently generated official AD samples.

The reference was made by the unmodified official chameleon converter, not
our exporter. Placed document coordinates are normalized to component origin.
"""
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_schlib, export_pcblib
from eda_geometry import points
from errors import DownloadError

if sys.platform == 'win32':
    from altium_inspect import schematic, pcb, region


def cases():
    with gzip.open(Path(__file__).parent / 'fixtures/official_ad_cases.json.gz', 'rt', encoding='utf-8') as f:
        return json.load(f)['cases']


def coordinate(row, key):
    return float(row.get(key, 0)) + float(row.get(key + '_FRAC', 0)) / 100000


def mil(value):
    return float(str(value).removesuffix('mil')) * 10000


def segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / (dx*dx+dy*dy))) if dx or dy else 0
    return math.dist(p, (a[0]+t*dx, a[1]+t*dy))


def contour_error(a, b):
    def directed(a, b):
        edges = list(zip(b, b[1:] + b[:1]))
        return max(min(segment_distance(p, x, y) for x, y in edges) for p in a)
    return max(directed(a, b), directed(b, a))


@unittest.skipUnless(sys.platform == 'win32', 'native AD libraries require Windows')
class OfficialAltiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = cases()
        cls.generated = {c['part']: (schematic(export_schlib(c['data'], c['part'])),
                                     pcb(export_pcblib(c['data'], c['part']))) for c in cls.samples}

    def test_all_symbol_units_and_pin_geometry_match_official_documents(self):
        self.assertEqual(len(self.samples), 27)
        for case in self.samples:
            with self.subTest(part=case['part']):
                header, _, pins = self.generated[case['part']][0]
                component = next(r for r in case['schematic'] if r.get('RECORD') == '1')
                reference = [r for r in case['schematic'] if r.get('RECORD') == '2']
                self.assertEqual(header['PARTCOUNT0'], component['PARTCOUNT'])
                self.assertEqual(len(pins), len(reference))
                expected = {(int(r['OWNERPARTID']), r['DESIGNATOR']): r for r in reference}
                declarations = {}
                for unit_index, unit in enumerate(case['data'].get('subparts') or [case['data']], 1):
                    for shape in unit['dataStr']['shape']:
                        if shape.startswith('P~'):
                            groups = [g.split('~') for g in shape.split('^^')]
                            declarations[unit_index, groups[4][4]] = int(groups[0][2] or 0)
                self.assertEqual(Counter((p['part'], p['number']) for p in pins),
                                 Counter((int(r['OWNERPARTID']), r['DESIGNATOR']) for r in reference))
                for pin in pins:
                    r = expected[pin['part'], pin['number']]
                    self.assertEqual(pin['name'], r['NAME'])
                    # The official Standard->Pro->AD samples replace electrical
                    # types with Passive. Preserve the original JSON declaration.
                    self.assertEqual(pin['electrical'], {0:4, 1:0, 2:1, 3:2, 4:7}[declarations[pin['part'], pin['number']]])
                    self.assertEqual(pin['flags'], int(r['PINCONGLOMERATE']))
                    for key, field in (('x', 'LOCATION.X'), ('y', 'LOCATION.Y'), ('length', 'PINLENGTH')):
                        value = coordinate(r, field) - (coordinate(component, field) if key != 'length' else 0)
                        self.assertAlmostEqual(pin[key], value, places=5)

    def test_symbol_canvas_has_a_white_background_and_a_valid_editing_grid(self):
        for case in self.samples:
            with self.subTest(part=case['part']):
                header, _, _ = self.generated[case['part']][0]
                self.assertEqual(int(header['AREACOLOR']), 0xffffff)
                self.assertEqual(header['SNAPGRIDON'], 'T')
                self.assertEqual(header['VISIBLEGRIDON'], 'T')
                self.assertGreater(float(header['SNAPGRIDSIZE']), 0)
                self.assertGreater(float(header['VISIBLEGRIDSIZE']), 0)
                self.assertIn('FONTNAME' + header['SYSTEMFONT'], header)
                # Captured from a blank library saved by Altium Designer 26.7.1;
                # these are editor settings, not graphical symbol primitives.
                for key, expected in {'BORDERON': 'T', 'USECUSTOMSHEET': 'T', 'REFERENCEZONESON': 'T',
                                      'CUSTOMX': '18000', 'CUSTOMY': '18000',
                                      'SHEETNUMBERSPACESIZE': '12', 'DISPLAY_UNIT': '0'}.items():
                    self.assertEqual(header[key], expected)

    def test_pin_text_keeps_source_colors_fonts_and_native_visibility(self):
        def source_color(value):
            digits = value.lstrip('#')
            if len(digits) == 3:
                digits = ''.join(digit * 2 for digit in digits)
            return int.from_bytes(bytes.fromhex(digits), 'little')

        for case in self.samples:
            header, records, _ = self.generated[case['part']][0]
            actual = {(int(r['OWNERPARTID']), r['DESIGNATOR']): r
                      for r in records if r.get('RECORD') == '2'}
            for unit_index, unit in enumerate(case['data'].get('subparts') or [case['data']], 1):
                for shape in unit['dataStr']['shape']:
                    if not shape.startswith('P~'):
                        continue
                    groups = [g.split('~') for g in shape.split('^^')]
                    row = actual[unit_index, groups[4][4]]
                    with self.subTest(part=case['part'], unit=unit_index, pin=row['DESIGNATOR']):
                        self.assertEqual(row['FORMALTYPE'], '1')
                        for prefix, group, bit in [('NAME', groups[3], 8), ('DESIGNATOR', groups[4], 16)]:
                            font_id = row[prefix + '_CUSTOMFONTID']
                            self.assertEqual(header['FONTNAME' + font_id], group[6] or 'Verdana')
                            self.assertEqual(int(header['SIZE' + font_id]),
                                             round(float((group[7] or '7pt').removesuffix('pt'))))
                            self.assertEqual(int(row[prefix + '_CUSTOMCOLOR']), source_color(group[8]))
                            # Bit 4 enables the custom font; position stays in
                            # Altium's automatic mode and text remains a pin attribute.
                            self.assertEqual(int(row['PIN' + prefix + '_POSITIONCONGLOMERATE']) & 16, 16)
                            self.assertEqual(bool(int(row['PINCONGLOMERATE']) & bit), group[0] == '1')

    def test_type_c_ground_pins_remain_visible_on_the_white_canvas(self):
        header, records, pins = self.generated['C165948'][0]
        self.assertEqual(len(pins), 16)
        grounds = [r for r in records if r.get('RECORD') == '2' and r['NAME'] == 'GND']
        self.assertEqual({r['DESIGNATOR'] for r in grounds}, {'A1B12', 'B1A12'})
        for row in grounds:
            self.assertEqual(int(row['PINCONGLOMERATE']) & 28, 24)
            self.assertEqual(int(row['COLOR']), 0)
            self.assertNotEqual(row['NAME_CUSTOMCOLOR'], header['AREACOLOR'])

    def test_native_symbol_body_coordinates_match_official_documents(self):
        for case in self.samples:
            with self.subTest(part=case['part']):
                _, actual, _ = self.generated[case['part']][0]
                comp = next(r for r in case['schematic'] if r.get('RECORD') == '1')
                # Official documents also encode square rectangles as rounded
                # rectangles with zero radii. Both native forms are equivalent.
                for kinds in (('8',), ('10', '14')):
                    refs = [r for r in case['schematic'] if r.get('RECORD') in kinds]
                    rows = [r for r in actual if r.get('RECORD') in kinds]
                    self.assertEqual(len(rows), len(refs))
                    for row, ref in zip(rows, refs):
                        self.assertEqual(row['OWNERPARTID'], ref['OWNERPARTID'])
                        for field in ('LOCATION.X', 'LOCATION.Y', 'CORNER.X', 'CORNER.Y', 'RADIUS', 'SECONDARYRADIUS',
                                      'CORNERXRADIUS', 'CORNERYRADIUS'):
                            if field not in ref:
                                continue
                            offset = coordinate(comp, 'LOCATION.' + field[-1]) if field.endswith(('.X', '.Y')) else 0
                            self.assertAlmostEqual(coordinate(row, field), coordinate(ref, field) - offset, places=5)

    def test_standard_pads_keep_official_dimensions_drills_masks_and_offsets(self):
        layers = {'TOP': 1, 'BOTTOM': 32, 'MULTILAYER': 74}
        for case in self.samples:
            with self.subTest(part=case['part']):
                _, pads, _ = self.generated[case['part']][1]
                src = case['data']['packageDetail']['dataStr']['shape']
                self.assertEqual(len(pads), sum(s.split('~')[0] in ('PAD', 'HOLE') for s in src))
                ref_pads = [r for r in case['pcb'] if r.get('RECORD') == 'Pad']
                self.assertEqual(Counter(p['number'] for p in pads if p['number']), Counter(r['NAME'] for r in ref_pads if r['NAME']))
                comp = next(r for r in case['pcb'] if r.get('RECORD') == 'Component')
                ox, oy = mil(comp['X']), mil(comp['Y'])
                # Polygon anchors can differ while their full copper contour is
                # equivalent. Standard-pad geometry is compared directly.
                source_index = 0
                for shape in src:
                    f = shape.split('~')
                    if f[0] not in ('PAD', 'HOLE'):
                        continue
                    pad = pads[source_index]; source_index += 1
                    if f[0] == 'HOLE':
                        self.assertFalse(pad['plated'])
                        self.assertEqual(pad['drill'], pad['width'])
                        continue
                    if f[1] == 'POLYGON':
                        continue
                    matches = [r for r in ref_pads if r['NAME'] == pad['number'] and layers[r['LAYER']] == pad['layer']]
                    self.assertTrue(matches)
                    ref = min(matches, key=lambda r: math.dist((mil(r['X'])-ox, mil(r['Y'])-oy), (pad['x'], pad['y'])))
                    for field, value in [('x', mil(ref['X'])-ox), ('y', mil(ref['Y'])-oy),
                                         ('width', mil(ref['XSIZE'])), ('height', mil(ref['YSIZE'])),
                                         ('drill', mil(ref['HOLESIZE'])), ('slot', mil(ref['HOLEWIDTH']) if ref['HOLETYPE'] == '2' else 0),
                                         ('paste', mil(ref.get('CPE', '0mil'))), ('solder', mil(ref.get('CSE', '0mil'))),
                                         ('offset_x', mil(ref.get('PADXOFFSET0', '0mil'))), ('offset_y', mil(ref.get('PADYOFFSET0', '0mil')))]:
                        # The official converter rounds some coordinates to
                        # 0.001 mil; our output retains the original JSON digits.
                        self.assertAlmostEqual(pad[field], value, delta=11, msg=f"{case['part']} pad {pad['number']} {field}")
                    self.assertEqual(pad['plated'], ref['PLATED'] == 'TRUE')
                    self.assertAlmostEqual(pad['rotation'] % 360, float(ref['ROTATION']) % 360, places=5)
                    if pad['slot']:
                        self.assertAlmostEqual(pad['hole_rotation'] % 180, float(ref['HOLEROTATION']) % 180, places=5)

    def test_custom_copper_contours_and_pad_associations_survive_serialization(self):
        layers = {'TOP': 1, 'BOTTOM': 32, 'MULTILAYER': 74}
        for case in self.samples:
            _, pads, other = self.generated[case['part']][1]
            by_primitive = {p['primitive_index'] + 1: p for p in pads}
            custom = [(layer, meta, contours[0]) for kind, body in other if kind == 11
                      for layer, meta, contours in [region(body)] if 'PADINDEX' in meta]
            comp = next(r for r in case['pcb'] if r.get('RECORD') == 'Component')
            ox, oy = mil(comp['X']), mil(comp['Y'])
            candidates = []
            for ref in case['pcb']:
                if ref.get('RECORD') == 'Region' and ref['LAYER'] in layers:
                    vertices = [(mil(ref[f'VX{i}'])-ox, mil(ref[f'VY{i}'])-oy)
                                for i in range(int(ref['MAINCONTOURVERTEXCOUNT']))]
                    candidates.append((layers[ref['LAYER']], vertices))
                elif ref.get('RECORD') == 'Pad' and ref.get('SHAPE') == 'RECTANGLE':
                    angle = math.radians(float(ref['ROTATION']))
                    cx, cy = mil(ref['X'])-ox, mil(ref['Y'])-oy
                    w, h = mil(ref['XSIZE'])/2, mil(ref['YSIZE'])/2
                    dx, dy = mil(ref.get('PADXOFFSET0', '0mil')), mil(ref.get('PADYOFFSET0', '0mil'))
                    vertices = [(cx+(x+dx)*math.cos(angle)-(y+dy)*math.sin(angle),
                                 cy+(x+dx)*math.sin(angle)+(y+dy)*math.cos(angle)) for x,y in [(-w,-h),(w,-h),(w,h),(-w,h)]]
                    candidates.append((layers[ref['LAYER']], vertices))
            source_pads = [s.split('~') for s in case['data']['packageDetail']['dataStr']['shape']
                           if s.split('~')[0] in ('PAD', 'HOLE')]
            custom_indices = {int(meta['PADINDEX']) for _, meta, _ in custom}
            for source, pad in zip(source_pads, pads):
                if source[:2] != ['PAD', 'POLYGON']:
                    continue
                with self.subTest(part=case['part'], pad=pad['number']):
                    owner = pad['primitive_index'] + 1
                    raw = [((px-float(case['data']['packageDetail']['dataStr']['head']['x']))*100000,
                            (float(case['data']['packageDetail']['dataStr']['head']['y'])-py)*100000)
                           for px, py in points(source[10])]
                    if owner in custom_indices:
                        rows = [(layer, meta, v) for layer, meta, v in custom if int(meta['PADINDEX']) == owner]
                        self.assertEqual({layer for layer, _, _ in rows},
                                         {1, 2, 32} if pad['layer'] == 74 else {pad['layer']})
                        self.assertTrue(all(contour_error(v, raw) <= 1 for _, _, v in rows))
                        self.assertTrue(all(float(meta['SOLDERMASKEXPANSION_MANUAL'].removesuffix('mil'))*10000 == pad['solder']
                                            for _, meta, _ in rows))
                    else:
                        # A recognized rectangle is a full native Pad, with no
                        # circular anchor or extra custom copper region.
                        self.assertEqual(pad['shape'], 2)
                        angle = math.radians(pad['rotation'])
                        w, h = pad['width']/2, pad['height']/2
                        v = [(pad['x']+x*math.cos(angle)-y*math.sin(angle),
                              pad['y']+x*math.sin(angle)+y*math.cos(angle))
                             for x, y in [(-w,-h),(w,-h),(w,h),(-w,h)]]
                        self.assertLessEqual(contour_error(v, raw), 500)
                        self.assertEqual(pad['paste'], round(float(source[17])*100000))
                        self.assertEqual(pad['solder'], round(float(source[18])*100000))
            for layer, meta, vertices in custom:
                with self.subTest(part=case['part'], pad=meta['PADINDEX'], layer=layer):
                    self.assertIn(int(meta['PADINDEX']), by_primitive)
                    self.assertIn('SOLDERMASKEXPANSION_MANUAL', meta)
                    self.assertIn('PASTEMASKEXPANSION_MANUAL', meta)
                    self.assertTrue(any((l == layer or l == 74 and layer in (1,2,32))
                                        and contour_error(vertices, v) <= 500 for l,v in candidates))

    def test_polygon_rectangles_become_full_native_pads_on_either_board_side(self):
        for part, names, layer in [('C165948', {'A1B12','A4B9','B1A12','B4A9'}, 1),
                                    ('C70373', {'1'}, 32)]:
            _, pads, other = self.generated[part][1]
            selected = [pad for pad in pads if pad['number'] in names and pad['layer'] == layer]
            self.assertEqual(len(selected), 4 if part == 'C165948' else 2)
            self.assertTrue(all(p['shape'] == 2 and p['layer'] == layer for p in selected))
            self.assertFalse([body for kind, body in other if kind == 11 and 'PADINDEX' in region(body)[1]])
            if part == 'C165948':
                for pad in selected:
                    self.assertAlmostEqual(pad['width'] / 10000 * .0254, .6, delta=.0002)
                    self.assertAlmostEqual(pad['height'] / 10000 * .0254, 1.3, delta=.0002)

    def test_polarity_marker_disks_keep_their_vendor_radius(self):
        for case in self.samples:
            _, _, other = self.generated[case['part']][1]
            regions = [contours[0] for kind, body in other if kind == 11
                       for layer, meta, contours in [region(body)] if layer == 67]
            doc = case['data']['packageDetail']['dataStr']; head = doc['head']
            for shape in doc['shape']:
                f = shape.split('~')
                if f[0] != 'CIRCLE' or f[5] != '101' or float(f[4]) < 2*float(f[3]):
                    continue
                cx, cy = (float(f[1])-float(head['x']))*100000, (float(head['y'])-float(f[2]))*100000
                radius = float(f[3])*100000
                vertices = min(regions, key=lambda v: math.dist((sum(p[0] for p in v)/len(v), sum(p[1] for p in v)/len(v)), (cx,cy)))
                for axis, center in [(0,cx),(1,cy)]:
                    self.assertAlmostEqual(min(p[axis] for p in vertices), center-radius, delta=501)
                    self.assertAlmostEqual(max(p[axis] for p in vertices), center+radius, delta=501)

    def test_invalid_custom_pad_does_not_silently_become_a_rectangle(self):
        data = json.loads(json.dumps(self.samples[0]['data']))
        doc = data['packageDetail']['dataStr']
        f = next(s for s in doc['shape'] if s.startswith('PAD~POLYGON')).split('~')
        f[10] = ''
        doc['shape'] = ['~'.join(f)]
        with self.assertRaises(DownloadError):
            export_pcblib(data, self.samples[0]['part'])
