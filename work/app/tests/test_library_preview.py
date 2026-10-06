import copy
import json
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library_preview import build_library_preview

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(part):
    return json.loads((FIXTURES / f'{part}.json').read_text(encoding='utf-8'))


class LibraryPreviewTests(unittest.TestCase):
    def test_real_symbols_and_footprints_keep_pin_and_pad_geometry(self):
        for part, count in (('C2040', 57), ('C20197', 8)):
            with self.subTest(part=part):
                data = fixture(part)
                original = copy.deepcopy(data)
                preview = build_library_preview(data)
                self.assertEqual(data, original, 'Preview changed the export input')
                self.assertEqual(preview.symbols[0].count, count)
                self.assertEqual(preview.footprint.count, count)
                self.assertFalse(preview.symbols[0].error)
                self.assertFalse(preview.footprint.error)
                root = ET.fromstring(preview.symbols[0].svg)
                paths = {node.get('d') for node in root.iter('{http://www.w3.org/2000/svg}path')}
                for shape in data['dataStr']['shape']:
                    if shape.startswith('P~'):
                        self.assertIn(shape.split('^^')[2].split('~')[0], paths)
                pads = [shape.split('~') for shape in data['packageDetail']['dataStr']['shape']
                        if shape.startswith('PAD~')]
                root = ET.fromstring(preview.footprint.svg)
                numbers = {node.text: (float(node.get('x')), float(node.get('y')))
                           for node in root.iter('{http://www.w3.org/2000/svg}text')}
                self.assertEqual(set(numbers), {str(index) for index in range(1, count + 1)})
                for pad in pads:
                    self.assertEqual(numbers[pad[8]][0], float(pad[2]))
                    self.assertLess(abs(numbers[pad[8]][1] - float(pad[3])), min(float(pad[4]), float(pad[5])) / 2)
                for text in root.iter('{http://www.w3.org/2000/svg}text'):
                    pad = next(pad for pad in pads if pad[8] == text.text)
                    self.assertLess(float(text.get('font-size')) * max(1, len(text.text)) * 0.65,
                                    min(float(pad[4]), float(pad[5])))

    def test_missing_symbol_does_not_block_footprint_and_reverse(self):
        data = fixture('C2040')
        data.pop('dataStr')
        preview = build_library_preview(data)
        self.assertIn('没有符号数据', preview.symbols[0].error)
        self.assertEqual(preview.footprint.count, 57)
        data = fixture('C2040')
        data['packageDetail'] = None
        preview = build_library_preview(data)
        self.assertEqual(preview.symbols[0].count, 57)
        self.assertIn('没有封装数据', preview.footprint.error)

    def test_multi_unit_symbols_keep_separate_pins(self):
        data = fixture('C20197')
        units = []
        for numbers in ({'1', '2', '7', '8'}, {'3', '4', '5', '6'}):
            block = copy.deepcopy(data['dataStr'])
            block['shape'] = [shape for shape in block['shape']
                              if not shape.startswith('P~') or shape.split('~')[3] in numbers]
            units.append({'dataStr': block})
        data['subparts'] = units
        preview = build_library_preview(data)
        self.assertEqual(len(preview.symbols), 2)
        self.assertEqual([unit.count for unit in preview.symbols], [4, 4])
        self.assertNotEqual(preview.symbols[0].svg, preview.symbols[1].svg)

    def test_empty_or_unknown_geometry_is_not_reported_as_ready(self):
        preview = build_library_preview({'dataStr': {'shape': ['UNKNOWN~test']},
                                         'packageDetail': {'dataStr': {'shape': ['SVGNODE~{}']}}})
        self.assertTrue(preview.symbols[0].error)
        self.assertEqual(preview.symbols[0].unsupported, ('UNKNOWN',))
        self.assertTrue(preview.footprint.error)


if __name__ == '__main__':
    unittest.main()
