"""Verify multi-component native libraries and batch commit behaviour."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_schlib, export_pcblib, MergedLibrary
from backend import NetworkApi, Options, download_batch, library_filename
from errors import DownloadError
from app_logging import close_logging
from altium_inspect import schematic, pcb, Reader, parameters, merged_pcb_section
import olefile


def fixture(part):
    return json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))


@unittest.skipUnless(sys.platform == 'win32', 'Windows native library export')
class MergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'appdata')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        self.parts = ['C2040', 'C20197', 'C2765186']
        self.data = {part: fixture(part) for part in self.parts}
        self.api = NetworkApi()
        self.api.get_cad_data_of_component = lambda part: copy.deepcopy(self.data[part])
        self.options = Options(self.root / 'export', ('SCHLIB', 'PCBLIB'), True, True, '项目符号.SchLib', '项目封装')

    def batch(self, **kwargs):
        return download_batch(self.parts, self.options, self.api.cancelled, api=self.api, **kwargs)

    def test_both_libraries_preserve_every_component_geometry_fonts_and_references(self):
        results = self.batch()
        self.assertEqual([r.status for r in results], ['成功'] * 3)
        files = list(self.options.destination.iterdir())
        self.assertEqual({p.name for p in files}, {'项目符号.SchLib', '项目封装.PcbLib'})
        sch = (self.options.destination / '项目符号.SchLib').read_bytes()
        board = (self.options.destination / '项目封装.PcbLib').read_bytes()
        with olefile.OleFileIO(io.BytesIO(sch)) as ole:
            keys = parameters(Reader(ole.openstream('SectionKeys').read()).block()[1])
            self.assertEqual(keys['KEYCOUNT'], '3')
            self.assertEqual([keys[f'SECTIONKEY{i}'] for i in range(3)], [p + '_Symbol' for p in self.parts])
        with olefile.OleFileIO(io.BytesIO(board)) as ole:
            self.assertEqual(struct.unpack('<I', ole.openstream('Library/Header').read())[0], 1)
            data = Reader(ole.openstream('Library/Data').read())
            data.block()
            self.assertEqual(data.unpack('<I')[0], 3)
            names = [Reader(data.block()[1]).string() for _ in range(3)]
            sections = Reader(ole.openstream('SectionKeys').read())
            self.assertEqual(sections.unpack('<I')[0], 3)
            for part, name in zip(self.parts, names):
                self.assertEqual(Reader(sections.block()[1]).string(), name)
                self.assertEqual(Reader(sections.block()[1]).string(), name)
        for index, part in enumerate(self.parts):
            header, records, pins = schematic(sch, part + '_Symbol')
            old_header, old_records, old_pins = schematic(export_schlib(self.data[part], part))
            self.assertEqual(header['COMPCOUNT'], '3')
            self.assertEqual(pins, old_pins)
            self.assertEqual(header[f'LIBREF{index}'], records[0]['LIBREFERENCE'])
            for record, original in zip(records, old_records):
                for key, value in record.items():
                    if key.endswith('FONTID'):
                        old_id = original[key]
                        self.assertEqual((header['FONTNAME' + value], header['SIZE' + value]),
                                         (old_header['FONTNAME' + old_id], old_header['SIZE' + old_id]))
            name, pads, primitives = pcb(board, merged_pcb_section(board, part))
            old_name, old_pads, old_primitives = pcb(export_pcblib(self.data[part], part))
            self.assertEqual(pads, old_pads)
            self.assertEqual(primitives, old_primitives)
            self.assertEqual(name, names[index])
            self.assertEqual(next(r['MODELNAME'] for r in records if r.get('RECORD') == '45'), name)
            self.assertEqual(len(results[index].files), 2)

    def test_each_merge_switch_works_independently_and_preserves_link(self):
        for sch_merge, pcb_merge in ((True, False), (False, True), (False, False)):
            with self.subTest(sch=sch_merge, pcb=pcb_merge):
                self.options.destination = self.root / f'{sch_merge}-{pcb_merge}'
                self.options.merge_schlib, self.options.merge_pcblib = sch_merge, pcb_merge
                for result in self.batch():
                    self.assertEqual(result.status, '成功')
                    sch_path = next(Path(f) for f in result.files if f.endswith('.SchLib'))
                    pcb_path = next(Path(f) for f in result.files if f.endswith('.PcbLib'))
                    _, records, _ = schematic(sch_path.read_bytes(), result.part + '_Symbol' if sch_merge else None)
                    board = pcb_path.read_bytes()
                    name, _, _ = pcb(board, merged_pcb_section(board, result.part) if pcb_merge else None)
                    self.assertEqual(next(r['MODELNAME'] for r in records if r.get('RECORD') == '45'), name)
                    self.assertEqual(sch_path.parent == self.options.destination, sch_merge)
                    self.assertEqual(pcb_path.parent == self.options.destination, pcb_merge)

    def test_same_titles_long_names_and_shared_footprints_never_overwrite_entries(self):
        for part in self.parts:
            self.data[part] = fixture('C20197')
            self.data[part]['title'] = 'Duplicate' * 20
        results = self.batch()
        sch = Path(results[0].files[0]).read_bytes()
        board = Path(results[0].files[1]).read_bytes()
        names = []
        for part in self.parts:
            _, records, pins = schematic(sch, part + '_Symbol')
            name, pads, _ = pcb(board, merged_pcb_section(board, part))
            self.assertEqual((len(pins), len(pads)), (8, 8))
            self.assertEqual(next(r['MODELNAME'] for r in records if r.get('RECORD') == '45'), name)
            names.append(records[0]['LIBREFERENCE'])
        self.assertEqual(len(set(names)), 3)

    def test_multiunit_symbol_keeps_each_pin_in_its_original_unit(self):
        with gzip.open(Path(__file__).parent / 'fixtures/official_ad_cases.json.gz', 'rt', encoding='utf-8') as stream:
            cases = json.load(stream)['cases']
        case = next(case for case in cases if len(case['data'].get('subparts', [])) > 1)
        self.parts = [case['part'], 'C20197']
        self.data[case['part']] = case['data']
        results = self.batch()
        self.assertEqual(results[0].status, '成功')
        header, records, pins = schematic(Path(results[0].files[0]).read_bytes(), case['part'] + '_Symbol')
        _, original, original_pins = schematic(export_schlib(case['data'], case['part']))
        self.assertEqual(header['PARTCOUNT0'], original[0]['PARTCOUNT'])
        self.assertEqual(records[0]['PARTCOUNT'], original[0]['PARTCOUNT'])
        self.assertEqual(pins, original_pins)
        self.assertGreater(len({pin['part'] for pin in pins}), 1)

    def test_bad_component_does_not_discard_other_parts_or_other_format(self):
        self.data['C20197']['packageDetail']['dataStr']['shape'].append('UNSUPPORTED~1')
        results = self.batch()
        self.assertEqual([r.status for r in results], ['成功', '部分完成', '成功'])
        with olefile.OleFileIO(results[0].files[1]) as ole:
            data = Reader(ole.openstream('Library/Data').read())
            data.block()
            self.assertEqual(data.unpack('<I')[0], 2)
            self.assertFalse(any(path[0].endswith('_C20197') for path in ole.listdir()))
        self.assertIn('PCBLIB', results[1].message)

    def test_cancel_before_commit_keeps_old_libraries_and_reports_no_success(self):
        self.options.destination.mkdir()
        for path in self.options.merged_paths().values():
            path.write_bytes(b'old library')
        def on_result(index, result):
            if result.pending_formats:
                self.api.cancelled.set()
        results = self.batch(on_result=on_result, workers=1)
        self.assertTrue(all(r.status == '已取消' and not r.files for r in results))
        self.assertTrue(all(path.read_bytes() == b'old library' for path in self.options.merged_paths().values()))

    def test_write_failure_reports_partial_and_keeps_existing_file(self):
        self.options.destination.mkdir()
        old = self.options.destination / '项目封装.PcbLib'
        old.write_bytes(b'old footprint')
        from backend import atomic_write
        def write(path, data):
            if path.suffix == '.PcbLib':
                raise PermissionError('locked')
            atomic_write(path, data)
        with patch('backend.atomic_write', side_effect=write):
            results = self.batch()
        self.assertEqual([r.status for r in results], ['部分完成'] * 3)
        self.assertTrue(all('合并失败' in r.message and len(r.files) == 1 for r in results))
        self.assertEqual(old.read_bytes(), b'old footprint')

    def test_new_batch_replaces_instead_of_accumulating_old_members(self):
        self.batch()
        self.parts = ['C20197']
        result = self.batch()[0]
        header, _, _ = schematic(Path(result.files[0]).read_bytes())
        self.assertEqual(header['COMPCOUNT'], '1')
        with olefile.OleFileIO(result.files[1]) as ole:
            self.assertFalse(any(path[0].endswith('_C2040') for path in ole.listdir()))
            self.assertTrue(any(path[0].endswith('_C20197') for path in ole.listdir()))

    def test_invalid_names_fail_before_fetch_and_unselected_merge_is_ignored(self):
        for name in ('', ' ', '../escape', 'x/y', 'x\\y', 'CON.txt', 'NUL', 'COM1', 'LPT¹', '.', 'x.', 'x' * 116):
            with self.subTest(name=name), self.assertRaises(DownloadError):
                library_filename(name, 'SCHLIB')
        self.assertEqual(library_filename(' 项目.schlib ', 'SCHLIB'), '项目.SchLib')
        self.options.schlib_name = '../escape'
        with patch.object(self.api, 'get_cad_data_of_component') as fetch:
            with self.assertRaises(DownloadError):
                self.batch()
            fetch.assert_not_called()
        self.options.formats = ('PCBLIB',)
        self.assertTrue(all(r.status == '成功' for r in self.batch()))


if __name__ == '__main__':
    unittest.main()
