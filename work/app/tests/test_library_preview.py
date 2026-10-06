import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library_preview import build_library_preview

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(part):
    return json.loads((FIXTURES / f'{part}_svgs.json').read_text(encoding='utf-8'))


class LibraryPreviewTests(unittest.TestCase):
    def test_real_official_svgs_keep_geometry_text_and_css_unchanged(self):
        for part, count in (('C2040', 57), ('C20197', 8)):
            with self.subTest(part=part):
                data = fixture(part)
                data['source_url'] = f'https://lceda.cn/api/products/{part}/svgs'
                original = copy.deepcopy(data)
                preview = build_library_preview(data, part)
                self.assertEqual(data, original)
                self.assertEqual(preview.symbols[0].count, count)
                self.assertEqual(preview.footprint.count, count)
                self.assertFalse(preview.symbols[0].error)
                self.assertFalse(preview.footprint.error)
                self.assertEqual(preview.source_url, data['source_url'])
                self.assertEqual(preview.symbols[0].svg, data['result'][0]['svg'].encode('utf-8'))
                self.assertEqual(preview.footprint.svg, data['result'][1]['svg'].encode('utf-8'))
                self.assertTrue(preview.title)
                self.assertTrue(preview.footprint.name)

    def test_missing_symbol_does_not_block_footprint_and_reverse(self):
        data = fixture('C2040')
        data['result'] = [entry for entry in data['result'] if entry['docType'] != 2]
        preview = build_library_preview(data)
        self.assertIn('没有符号 SVG 数据', preview.symbols[0].error)
        self.assertEqual(preview.footprint.count, 57)
        data = fixture('C2040')
        data['result'] = [entry for entry in data['result'] if entry['docType'] != 4]
        preview = build_library_preview(data)
        self.assertEqual(preview.symbols[0].count, 57)
        self.assertIn('没有封装 SVG 数据', preview.footprint.error)

    def test_multi_unit_symbols_preferred_like_the_storefront(self):
        data = fixture('C20197')
        first = {**data['result'][0], 'docType': 6}
        second = {**fixture('C2040')['result'][0], 'docType': 6}
        data['result'].extend([first, second])
        preview = build_library_preview(data)
        self.assertEqual(len(preview.symbols), 2)
        self.assertEqual([unit.count for unit in preview.symbols], [8, 57])
        self.assertEqual(preview.symbols[0].svg, first['svg'].encode('utf-8'))
        self.assertEqual(preview.symbols[1].svg, second['svg'].encode('utf-8'))
        self.assertEqual(preview.footprint.count, 8)

    def test_malformed_symbol_does_not_block_valid_footprint(self):
        for svg in (None, '', '<html/>', '<svg', '<svg xmlns="http://www.w3.org/2000/svg"><title>empty</title></svg>'):
            with self.subTest(svg=svg):
                data = fixture('C20197')
                data['result'][0]['svg'] = svg
                preview = build_library_preview(data)
                self.assertTrue(preview.symbols[0].error)
                self.assertEqual(preview.footprint.count, 8)
                self.assertFalse(preview.footprint.error)

    def test_empty_response_and_unrelated_documents_are_not_ready(self):
        for result in (None, [], {}, [None, {}, {'docType': 3, 'svg': '<svg/>'}]):
            with self.subTest(result=result):
                preview = build_library_preview({'result': result}, 'C2040')
                self.assertTrue(preview.symbols[0].error)
                self.assertTrue(preview.footprint.error)
                self.assertEqual(preview.title, 'C2040')


if __name__ == '__main__':
    unittest.main()
