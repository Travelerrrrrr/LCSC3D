"""Native AD defaults, Passive pins and outward pin-length normalization."""
import copy
from collections import Counter
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_schlib, export_linked_schlib
from altium_inspect import schematic, source_symbol_text_sizes, symbol_text_sizes
import backend
from library_merge import LibraryIndex
from library_preview import build_library_preview, styled_symbol
from symbol_style import AD_STYLE, normalize_symbol_style
from test_export_modes import ExportFixture
from test_official_altium import cases
from test_library_preview import fixture


APPEARANCE = {'COLOR', 'AREACOLOR', 'LINEWIDTH', 'FONTID', 'NAME_CUSTOMFONTID',
              'DESIGNATOR_CUSTOMFONTID', 'NAME_CUSTOMCOLOR', 'DESIGNATOR_CUSTOMCOLOR', 'UNIQUEID',
              'CORNERXRADIUS', 'CORNERYRADIUS', 'CORNERXRADIUS_FRAC', 'CORNERYRADIUS_FRAC'}


def semantics(records):
    # Compare everything except the requested appearance and pin-length changes.
    rows = [{k: '14' if k == 'RECORD' and v == '10' else v
             for k, v in row.items() if k not in APPEARANCE
             and not (row.get('RECORD') in ('10', '14') and k in ('ISSOLID', 'TRANSPARENT', 'LINESTYLE'))
             and not (row.get('RECORD') == '2' and k in ('PINLENGTH', 'PINLENGTH_FRAC'))} for row in records]
    return Counter(json.dumps(row, sort_keys=True) for row in rows)


def assert_ad_fonts(test, header, records):
    test.assertFalse(any(row.get('RECORD') == '10' for row in records))
    for row in records:
        for key in ('FONTID', 'NAME_CUSTOMFONTID', 'DESIGNATOR_CUSTOMFONTID'):
            if key in row:
                test.assertEqual(header['FONTNAME' + row[key]], 'Times New Roman')
                test.assertGreater(int(header['SIZE' + row[key]]), 0)
        if row.get('RECORD') == '2':
            test.assertEqual(row['ELECTRICAL'], '4')
            test.assertGreaterEqual(float(row['PINLENGTH']) + float(row.get('PINLENGTH_FRAC', 0)) / 100000, 20)
            test.assertEqual(row['COLOR'], '0')
            test.assertEqual(row['NAME_CUSTOMCOLOR'], '8388608')
            test.assertEqual(row['DESIGNATOR_CUSTOMCOLOR'], '8388608')
        if row.get('RECORD') in ('6', '7', '8', '10', '14'):
            test.assertEqual(row['COLOR'], '128')
            test.assertEqual(row['LINEWIDTH'], '1')
            if 'AREACOLOR' in row:
                test.assertIn(row['AREACOLOR'], ('128', '11599871'))
            if row['RECORD'] == '14':
                test.assertEqual((row['ISSOLID'], row['TRANSPARENT'], row['AREACOLOR']), ('T', 'T', '11599871'))


class SymbolStyleTests(unittest.TestCase):
    def test_missing_and_invalid_preferences_use_original_style(self):
        for value in (None, '', 'AD', 'unknown', {}, [], 123):
            self.assertEqual(normalize_symbol_style(value), 'jlc')
            self.assertEqual(backend.Options(Path('.'), symbol_style=value, bind_3d_models=False).symbol_style, 'jlc')
        self.assertEqual(normalize_symbol_style('ad'), 'ad')

    def test_transparency_pale_backgrounds_and_solid_marks_are_distinct(self):
        for source in ('none', 'NONE', ' none '):
            self.assertEqual(AD_STYLE.fill(source), source)
        for source in ('#fff', '#FFFFFF', '#ffffb0'):
            # The preset itself remains a pale background on repeated styling.
            self.assertEqual(AD_STYLE.fill(source), '#ffffb0')
        for source in ('#880000', '#000', '#000000'):
            self.assertEqual(AD_STYLE.fill(source), '#800000')

    def test_preview_style_does_not_mutate_source_svg_or_footprint(self):
        data = fixture('C2040')
        original = copy.deepcopy(data)
        preview = build_library_preview(data, 'C2040')
        document = styled_symbol(preview.symbols[0], 'ad')
        self.assertEqual(document.svg, preview.symbols[0].svg)
        self.assertEqual(document.count, 57)
        self.assertEqual(document.symbol_style, 'ad')
        self.assertEqual(preview.symbols[0].symbol_style, 'jlc')
        self.assertEqual(preview.footprint.symbol_style, 'jlc')
        self.assertEqual(styled_symbol(document, 'jlc'), preview.symbols[0])
        self.assertEqual(data, original)


@unittest.skipUnless(sys.platform == 'win32', 'Native AD libraries')
class SymbolLibraryStyleTests(unittest.TestCase):
    def test_ad_preserves_distinct_pin_name_number_and_label_sizes(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        shapes = data['dataStr']['shape']
        pin_index = next(i for i, shape in enumerate(shapes) if shape.startswith('P~'))
        groups = [group.split('~') for group in shapes[pin_index].split('^^')]
        groups[3][7], groups[4][7] = '5pt', '9pt'
        shapes[pin_index] = '^^'.join('~'.join(group) for group in groups)
        text_index = next(i for i, shape in enumerate(shapes) if shape.startswith('T~'))
        fields = shapes[text_index].split('~')
        fields[7] = '14pt'
        shapes[text_index] = '~'.join(fields)
        original = copy.deepcopy(data)
        for style in ('jlc', 'ad'):
            with self.subTest(style=style):
                header, records, _ = schematic(export_schlib(data, 'C2040', symbol_style=style))
                pin = next(row for row in records if row.get('RECORD') == '2')
                label = next(row for row in records if row.get('RECORD') == '4')
                self.assertEqual(header['SIZE' + pin['NAME_CUSTOMFONTID']], '5')
                self.assertEqual(header['SIZE' + pin['DESIGNATOR_CUSTOMFONTID']], '9')
                self.assertEqual(header['SIZE' + label['FONTID']], '14')
                self.assertEqual(symbol_text_sizes(header, records), source_symbol_text_sizes(data))
                self.assertEqual(data, original)

    def test_native_minimum_pin_length_preserves_body_position_and_longer_pins(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        index = next(i for i, shape in enumerate(data['dataStr']['shape']) if shape.startswith('P~'))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for length in (0.5, 10, 19.5, 20, 60.5):
                with self.subTest(direction=(dx, dy), length=length):
                    source = copy.deepcopy(data)
                    groups = source['dataStr']['shape'][index].split('^^')
                    groups[1] = f'{400 + dx * length}~{300 + dy * length}'
                    path = groups[2].split('~')
                    path[0] = f'M {400 + dx * length} {300 + dy * length} L 400 300'
                    groups[2] = '~'.join(path)
                    source['dataStr']['shape'][index] = '^^'.join(groups)
                    _, _, jlc = schematic(export_schlib(source, 'C2040', symbol_style='jlc'))
                    _, _, ad = schematic(export_schlib(source, 'C2040', symbol_style='ad'))
                    self.assertAlmostEqual(jlc[0]['length'], length)
                    self.assertAlmostEqual(ad[0]['length'], max(20, length))
                    self.assertEqual({k: v for k, v in jlc[0].items() if k not in ('length', 'color')},
                                     {k: v for k, v in ad[0].items() if k not in ('length', 'color')})

    def test_ad_rectangle_and_font_family_match_user_native_reference(self):
        reference = json.loads((Path(__file__).parent / 'fixtures/ad_native_defaults.json').read_text('utf-8'))
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        # Apply defaults regardless of the source rectangle's fill setting.
        for fill in ('none', '#FFFFFF', '#880000'):
            with self.subTest(source_fill=fill):
                source = copy.deepcopy(data)
                index = next(i for i, shape in enumerate(source['dataStr']['shape']) if shape.startswith('R~'))
                fields = source['dataStr']['shape'][index].split('~')
                fields[10] = fill
                source['dataStr']['shape'][index] = '~'.join(fields)
                header, records, pins = schematic(export_schlib(source, 'C2040', symbol_style='ad'))
                rect = next(r for r in records if r.get('RECORD') == '14')
                self.assertLess(records.index(rect), next(i for i, r in enumerate(records) if r.get('RECORD') == '2'))
                for key, value in reference['rectangle'].items():
                    self.assertEqual(rect.get(key), value, key)
                self.assertEqual(rect['LINEWIDTH'], '1')  # User changed Border from hairline to Small.
                for row in records:
                    for key in ('FONTID', 'NAME_CUSTOMFONTID', 'DESIGNATOR_CUSTOMFONTID'):
                        if key in row:
                            self.assertEqual(header['FONTNAME' + row[key]], reference['font']['FONTNAME1'])
                self.assertEqual(symbol_text_sizes(header, records), source_symbol_text_sizes(source))
                self.assertTrue(all(p['electrical'] == reference['pin']['electrical'] for p in pins))

    def test_both_styles_force_all_source_electrical_types_to_passive(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        pin_indices = [i for i, shape in enumerate(data['dataStr']['shape']) if shape.startswith('P~')]
        for index, electrical in zip(pin_indices, ('0', '1', '2', '3', '4', '', 'unsupported')):
            groups = data['dataStr']['shape'][index].split('^^')
            fields = groups[0].split('~')
            fields[2] = electrical
            groups[0] = '~'.join(fields)
            data['dataStr']['shape'][index] = '^^'.join(groups)
        original = copy.deepcopy(data)
        for style in ('jlc', 'ad'):
            with self.subTest(style=style):
                _, _, pins = schematic(export_schlib(data, 'C2040', symbol_style=style))
                self.assertEqual(len(pins), len(pin_indices))
                self.assertTrue(all(p['electrical'] == 4 for p in pins))
                self.assertEqual(data, original)

    def test_ad_uses_native_straight_rectangles_while_jlc_keeps_corner_radii(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C2040.json').read_text('utf-8'))
        shapes = data['dataStr']['shape']
        index = next(i for i, shape in enumerate(shapes) if shape.startswith('R~'))
        fields = shapes[index].split('~')
        fields[3:5] = ['2.5', '3.5']
        shapes[index] = '~'.join(fields)
        original = copy.deepcopy(data)
        _, jlc, _ = schematic(export_schlib(data, 'C2040', symbol_style='jlc'))
        _, ad, _ = schematic(export_schlib(data, 'C2040', symbol_style='ad'))
        rounded = next(row for row in jlc if row.get('RECORD') == '10')
        rectangle = next(row for row in ad if row.get('RECORD') == '14')
        self.assertEqual(rounded['CORNERXRADIUS'], '2')
        self.assertEqual(rounded['CORNERXRADIUS_FRAC'], '50000')
        self.assertFalse(any(row.get('RECORD') == '10' for row in ad))
        self.assertFalse(any(key.startswith(('CORNERXRADIUS', 'CORNERYRADIUS')) for key in rectangle))
        self.assertEqual(semantics(jlc), semantics(ad))
        self.assertEqual(data, original)

    def test_inductor_export_has_a_white_canvas_and_no_background_fill(self):
        data = json.loads((Path(__file__).parent / 'fixtures/C163691.json').read_text('utf-8'))
        for style in ('jlc', 'ad'):
            with self.subTest(style=style):
                header, records, pins = schematic(export_schlib(data, 'C163691', symbol_style=style))
                self.assertEqual(header['AREACOLOR'], '16777215')
                self.assertEqual(len(pins), 2)
                self.assertFalse(any('AREACOLOR' in row or row.get('ISSOLID') == 'T' for row in records))

    def test_all_official_symbols_keep_geometry_pins_units_and_parameters(self):
        samples = cases()
        for part in ('C23922', 'C8734'):
            data = json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text('utf-8'))
            samples.append(dict(part=part, data=data))
        for sample in samples:
            with self.subTest(part=sample['part']):
                original = copy.deepcopy(sample['data'])
                before = schematic(export_schlib(sample['data'], sample['part']))
                after = schematic(export_schlib(sample['data'], sample['part'], symbol_style='ad'))
                self.assertEqual(semantics(before[1]), semantics(after[1]))
                self.assertEqual([{k: v for k, v in p.items() if k not in ('color', 'length')} for p in before[2]],
                                 [{k: v for k, v in p.items() if k not in ('color', 'length')} for p in after[2]])
                self.assertTrue(all(p['electrical'] == 4 for p in before[2] + after[2]))
                for old, new in zip(before[2], after[2]):
                    self.assertAlmostEqual(new['length'], max(20, old['length']), places=5)
                self.assertEqual(before[0]['PARTCOUNT0'], after[0]['PARTCOUNT0'])
                assert_ad_fonts(self, after[0], after[1])
                self.assertEqual(symbol_text_sizes(after[0], after[1]), source_symbol_text_sizes(sample['data']))
                self.assertEqual(sample['data'], original)

    def test_original_style_is_explicitly_selectable_after_ad_export(self):
        data = cases()[0]['data']
        before = schematic(export_schlib(data, 'C2040'))
        export_schlib(data, 'C2040', symbol_style='ad')
        after = schematic(export_schlib(data, 'C2040', symbol_style='jlc'))
        strip = lambda rows: [{k: v for k, v in row.items() if k != 'UNIQUEID'} for row in rows]
        self.assertEqual(strip(before[1]), strip(after[1]))
        self.assertEqual(before[2], after[2])

    def test_linked_individual_symbol_uses_style_and_actual_footprint_name(self):
        data = cases()[0]['data']
        header, rows, _ = schematic(export_linked_schlib(
            data, 'C2040', lambda: None, symbol_style='ad', footprint_name='ExistingFootprint'))
        assert_ad_fonts(self, header, rows)
        self.assertEqual(symbol_text_sizes(header, rows), source_symbol_text_sizes(data))
        self.assertEqual(next(row['MODELNAME'] for row in rows if row.get('RECORD') == '45'),
                         'ExistingFootprint')


@unittest.skipUnless(sys.platform == 'win32', 'Native AD libraries')
class SymbolStyleExportTests(ExportFixture):
    def test_style_reaches_individual_merged_deferred_and_project_exports(self):
        for mode in ('individual', 'merge', 'deferred', 'project'):
            with self.subTest(mode=mode):
                root = self.root / mode
                extra = {}
                if mode == 'project':
                    extra = dict(append_mode=True, project_path=str(self.project(root / 'project')),
                                 keep_individual=True)
                options = backend.Options(root, ('SCHLIB', 'PCBLIB'),
                    merge_schlib=mode == 'merge', merge_pcblib=mode in ('merge', 'deferred'),
                    keep_schlib=True, keep_pcblib=True, symbol_style='ad', **extra, bind_3d_models=False)
                results = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
                self.assertEqual([r.status for r in results], ['成功', '成功'])
                files = {Path(f) for r in results for f in r.files if f.endswith('.SchLib')}
                self.assertTrue(files)
                for file in files:
                    payload = file.read_bytes()
                    index = LibraryIndex(payload, 'SCHLIB')
                    try:
                        sections = list(index.sections.values())
                    finally:
                        index.ole.close()
                    for section in sections:
                        header, rows, _ = schematic(payload, section)
                        assert_ad_fonts(self, header, rows)
                        part = next(row['TEXT'] for row in rows if row.get('NAME') == 'SupplierPart')
                        self.assertEqual(symbol_text_sizes(header, rows), source_symbol_text_sizes(self.data[part]))

    def test_append_keeps_existing_symbol_styles_and_merges_font_indices(self):
        targets = self.targets(self.root / 'append', ('SCHLIB', 'PCBLIB'))
        original = schematic(self.originals['SCHLIB'], 'C2765186_Symbol')
        options = backend.Options(self.root, ('SCHLIB', 'PCBLIB'), append_mode=True,
                                  symbol_style='ad', **targets, bind_3d_models=False)
        results = backend.download_batch(['C2040', 'C20197'], options, api=self.api)
        self.assertEqual([r.status for r in results], ['成功', '成功'])
        payload = options.merged_paths()['SCHLIB'].read_bytes()
        self.assertEqual(schematic(payload, 'C2765186_Symbol')[1:], original[1:])
        for part in ('C2040', 'C20197'):
            header, rows, _ = schematic(payload, part + '_Symbol')
            assert_ad_fonts(self, header, rows)
            self.assertEqual(symbol_text_sizes(header, rows), source_symbol_text_sizes(self.data[part]))


if __name__ == '__main__':
    unittest.main()
