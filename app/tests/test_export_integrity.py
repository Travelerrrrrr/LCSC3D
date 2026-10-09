"""No-fill compatibility, shared footprints and consistent symbol references."""
import copy
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
from altium import (export_schlib, export_pcblib, export_linked_pcblib, MergedLibrary,
                    _pcblib_component, _parameters, _string_block, _pcb_header)
from altium_inspect import schematic, pcb
from app_logging import close_logging
from compound_storage import compound_file
from errors import DownloadError
from library_merge import LibraryIndex, append_library, footprint_signature, Reader, parameters
from test_backend import STEP


def fixture(part):
    return json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))


@unittest.skipUnless(sys.platform == 'win32', 'Native AD export')
class ExportIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'profile')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        self.data = {part: fixture(part) for part in ('C23922', 'C8734', 'C20197')}
        self.api = backend.NetworkApi()
        self.api.get_cad_data_of_component = lambda part: copy.deepcopy(self.data[part])
        self.api.get_step_3d_model = lambda uuid: STEP

    def names(self, path, fmt):
        payload = path if isinstance(path, bytes) else Path(path).read_bytes()
        index = LibraryIndex(payload, fmt)
        try:
            return index.names[:]
        finally:
            index.ole.close()

    def assert_references(self, rows, merged_sch, merged_pcb):
        names = self.names(merged_pcb, 'PCBLIB')
        for row in rows:
            symbols = [Path(file) for file in row.files if file.endswith('.SchLib')]
            for symbol in symbols:
                section = row.part + '_Symbol' if symbol == merged_sch else None
                _, records, pins = schematic(symbol.read_bytes(), section)
                self.assertEqual(len(pins), 48)
                name = next(record['MODELNAME'] for record in records if record.get('RECORD') == '45')
                self.assertIn(name, names)
                self.assertEqual(next(record['TEXT'] for record in records if record.get('RECORD') == '41'
                                      and record.get('NAME') == 'Footprint'), name)
                if symbol != merged_sch:
                    standalone = next((Path(file) for file in row.files if file.endswith('.PcbLib')
                                       and Path(file) != merged_pcb), None)
                    if standalone:
                        self.assertEqual(self.names(standalone, 'PCBLIB'), [name])

    def test_real_C23922_retains_two_unfilled_units_all_48_pins_and_48_pads(self):
        self.assertEqual(sum('~NONE~' in shape for unit in self.data['C23922']['subparts']
                             for shape in unit['dataStr']['shape']), 2)
        header, records, pins = schematic(export_schlib(self.data['C23922'], 'C23922'))
        self.assertEqual(header['PARTCOUNT0'], '3')
        self.assertEqual(len(pins), 48)
        self.assertEqual({pin['part'] for pin in pins}, {1, 2})
        self.assertEqual({pin['number'] for pin in pins}, {str(number) for number in range(1, 49)})
        bodies = [record for record in records if record.get('RECORD') in ('10', '14')]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(record['TRANSPARENT'] == 'T' and record.get('ISSOLID') != 'T'
                            and 'AREACOLOR' not in record for record in bodies))
        self.assertEqual(len(pcb(export_pcblib(self.data['C23922'], 'C23922'))[1]), 48)

    def test_all_schematic_fill_shapes_accept_case_variants_without_changing_geometry(self):
        shapes = [
            'R~0~0~0~0~10~10~#880000~1~0~{fill}~test~0',
            'C~0~0~10~#880000~1~0~{fill}~test~0',
            'E~0~0~10~5~#880000~1~0~{fill}~test~0',
            'PL~0 0 10 0 10 10~#880000~1~0~{fill}~test~0',
            'PG~0 0 10 0 10 10~#880000~1~0~{fill}~test~0',
            'PT~M 0 0 L 10 0 L 10 10 Z~#880000~1~0~{fill}~test~0',
            'A~M 0 0 A 10 10 0 0 1 10 10~~#880000~1~0~{fill}~test~0',
        ]
        for shape in shapes:
            expected = None
            for fill in ('none', 'NONE', 'NoNe', ' none '):
                with self.subTest(shape=shape.split('~')[0], fill=fill):
                    data = copy.deepcopy(self.data['C20197'])
                    data['dataStr']['shape'].append(shape.format(fill=fill))
                    _, records, pins = schematic(export_schlib(data, 'C20197'))
                    geometry = [row for row in records if row.get('RECORD') in ('6', '7', '8', '10', '14')]
                    if expected is None:
                        expected = geometry
                    self.assertEqual(geometry, expected)
                    self.assertEqual(len(pins), 8)

    def test_invalid_colors_still_fail_and_real_fill_colors_are_retained(self):
        data = self.data['C20197']
        data['dataStr']['shape'].append('R~0~0~0~0~10~10~#880000~1~0~#00ff00~test~0')
        _, records, _ = schematic(export_schlib(data, 'C20197'))
        self.assertEqual(next(row for row in records if row.get('AREACOLOR') == '65280')['ISSOLID'], 'T')
        data['dataStr']['shape'][-1] = data['dataStr']['shape'][-1].replace('#00ff00', 'not-a-color')
        with self.assertRaises(DownloadError):
            export_schlib(data, 'C20197')

    def test_uppercase_no_fill_pcb_rectangle_remains_an_outline(self):
        data = self.data['C20197']
        data['packageDetail']['dataStr']['shape'].append('RECT~3982.75~2994.5~34.5~11~3~test~0~0.7874~none~~~')
        _, baseline_pads, baseline_shapes = pcb(export_pcblib(data, 'C20197'))
        data['packageDetail']['dataStr']['shape'][-1] = data['packageDetail']['dataStr']['shape'][-1].replace('none', 'NONE')
        _, pads, shapes = pcb(export_pcblib(data, 'C20197'))
        self.assertEqual((pads, shapes), (baseline_pads, baseline_shapes))
        self.assertEqual([kind for kind, body in shapes[-4:]], [4] * 4)

    def test_real_shared_footprint_ignores_random_pad_ids_and_part_metadata(self):
        entries = [_pcblib_component(self.data[part], part, lambda: None) for part in ('C23922', 'C8734')]
        self.assertNotEqual(entries[0][1], entries[1][1])
        self.assertEqual(footprint_signature(entries[0][1]), footprint_signature(entries[1][1]))
        self.assertIsNotNone(footprint_signature(entries[0][1]))

    def test_16_merge_and_append_combinations_share_one_footprint_with_valid_links(self):
        for append, merge_sch, keep_sch, keep_pcb in itertools.product((False, True), repeat=4):
            with self.subTest(append=append, merge_sch=merge_sch, keep_sch=keep_sch, keep_pcb=keep_pcb):
                root = self.root / f'{append}-{merge_sch}-{keep_sch}-{keep_pcb}'
                root.mkdir()
                target = root / 'existing.PcbLib'
                original = None
                if append:
                    original = export_linked_pcblib(self.data['C23922'], 'C23922', lambda: None,
                                                    footprint_name='Existing_LQFP48')
                    target.write_bytes(original)
                options = backend.Options(root, ('STEP', 'SCHLIB', 'PCBLIB'), merge_sch, True,
                    'symbols', 'footprints', keep_sch, keep_pcb, pcblib_target=str(target) if append else '')
                rows = backend.download_batch(['C23922', 'C8734'], options, api=self.api)
                self.assertEqual([row.status for row in rows], ['成功', '成功'])
                merged_pcb = options.merged_paths()['PCBLIB']
                self.assertEqual(len(self.names(merged_pcb, 'PCBLIB')), 1)
                self.assert_references(rows, options.merged_paths().get('SCHLIB'), merged_pcb)
                if append:
                    self.assertEqual(target.read_bytes(), original)
                self.assertTrue(all('复用' in row.message for row in rows))
                for row in rows:
                    for file in row.files:
                        path = Path(file)
                        if path.suffix in ('.SchLib', '.PcbLib'):
                            self.assertNotIn(row.part, path.name)
                            self.assertTrue(all(not any(part in name for part in ('C23922', 'C8734'))
                                                for name in self.names(path, path.suffix[1:].upper())))

    def test_same_name_different_pad_geometry_gets_separate_name_and_matching_reference(self):
        data = self.data['C8734']['packageDetail']['dataStr']
        i = next(i for i, shape in enumerate(data['shape']) if shape.startswith('PAD~'))
        fields = data['shape'][i].split('~')
        fields[4] = str(float(fields[4]) + 1)
        data['shape'][i] = '~'.join(fields)
        options = backend.Options(self.root, ('SCHLIB', 'PCBLIB'), True, True)
        rows = backend.download_batch(['C23922', 'C8734'], options, api=self.api)
        self.assertEqual([row.status for row in rows], ['成功', '成功'])
        board = options.merged_paths()['PCBLIB']
        names = self.names(board, 'PCBLIB')
        self.assertEqual(len(names), 2)
        self.assertTrue(names[1].endswith('_2'))
        self.assert_references(rows, options.merged_paths()['SCHLIB'], board)
        index = LibraryIndex(board.read_bytes(), 'PCBLIB')
        try:
            for row, name in zip(rows, names):
                _, records, _ = schematic(options.merged_paths()['SCHLIB'].read_bytes(), row.part + '_Symbol')
                self.assertEqual(next(record['MODELNAME'] for record in records if record.get('RECORD') == '45'), name)
                _, actual_pads, _ = pcb(board.read_bytes(), index.sections[name.casefold()])
                _, original_pads, _ = pcb(export_pcblib(self.data[row.part], row.part))
                self.assertEqual(actual_pads, original_pads)
        finally:
            index.ole.close()

    def test_append_same_name_different_geometry_keeps_original_and_adds_variant(self):
        original = export_linked_pcblib(self.data['C23922'], 'C23922', lambda: None)
        data = copy.deepcopy(self.data['C23922'])
        i = next(i for i, shape in enumerate(data['packageDetail']['dataStr']['shape']) if shape.startswith('PAD~'))
        fields = data['packageDetail']['dataStr']['shape'][i].split('~')
        fields[8] = '99'
        data['packageDetail']['dataStr']['shape'][i] = '~'.join(fields)
        added = export_linked_pcblib(data, 'C8734', lambda: None)
        resolved = {}
        result, skipped = append_library(original, added, 'PCBLIB', footprint_names=resolved)
        self.assertEqual(skipped, [])
        self.assertEqual(len(self.names(result, 'PCBLIB')), 2)
        old, new = LibraryIndex(original, 'PCBLIB'), LibraryIndex(result, 'PCBLIB')
        try:
            section = old.sections[old.names[0].casefold()]
            for path in old.ole.listdir():
                if path[0] == section:
                    self.assertEqual(old.read(path), new.read(path))
            self.assertTrue(next(iter(resolved.values())).endswith('_2'))
        finally:
            old.ole.close()
            new.ole.close()

    def test_physical_side_data_and_unknown_native_data_are_not_discarded_as_duplicates(self):
        name, streams = _pcblib_component(self.data['C20197'], 'C20197', lambda: None, merged=True)
        baseline = footprint_signature(streams)
        for variant in ('height', 'extra_stream', 'unknown_record', 'text'):
            with self.subTest(variant=variant):
                changed = dict(streams)
                if variant == 'height':
                    changed[name + '/Parameters'] = changed[name + '/Parameters'].replace(b'0mil', b'1mil')
                elif variant == 'extra_stream':
                    changed[name + '/Custom'] = b'preserve this data'
                elif variant == 'unknown_record':
                    changed[name + '/Data'] += b'\x0c' + _parameters({'ID': '3D body'})
                else:
                    changed[name + '/WideStrings'] = _parameters({'ENCODEDTEXT0': '65'})
                self.assertNotEqual(footprint_signature(changed), baseline)

    def test_direct_append_reuses_equivalent_renamed_footprint(self):
        original = export_linked_pcblib(self.data['C23922'], 'C23922', lambda: None,
                                        footprint_name='My_LQFP48')
        incoming = export_linked_pcblib(self.data['C8734'], 'C8734', lambda: None)
        resolved = {}
        result, skipped = append_library(original, incoming, 'PCBLIB', footprint_names=resolved)
        self.assertEqual(result, original)
        self.assertEqual(skipped, ['My_LQFP48'])
        self.assertEqual(list(resolved.values()), ['My_LQFP48'])

    def test_same_geometry_with_different_source_names_uses_one_name_in_both_symbols(self):
        self.data['C8734']['packageDetail']['title'] = 'Alternate_LQFP48'
        options = backend.Options(self.root, ('SCHLIB', 'PCBLIB'), True, True)
        rows = backend.download_batch(['C23922', 'C8734'], options, api=self.api)
        self.assertEqual([row.status for row in rows], ['成功', '成功'])
        self.assertEqual(len(self.names(options.merged_paths()['PCBLIB'], 'PCBLIB')), 1)
        self.assert_references(rows, options.merged_paths()['SCHLIB'], options.merged_paths()['PCBLIB'])

    def test_preexisting_duplicate_footprints_are_preserved_without_adding_another(self):
        import struct
        streams = {}
        for name in ('Old_LQFP_A', 'Old_LQFP_B'):
            _, component = _pcblib_component(self.data['C23922'], 'C23922', lambda: None, footprint_name=name)
            streams.update(component)
        streams['Library/Data'] = _parameters(_pcb_header()) + struct.pack('<I', 2) + b''.join(
            _string_block(name) for name in ('Old_LQFP_A', 'Old_LQFP_B'))
        streams['SectionKeys'] = struct.pack('<I', 2) + b''.join(_string_block(name) * 2
            for name in ('Old_LQFP_A', 'Old_LQFP_B'))
        original = compound_file(streams)
        self.assertEqual(len(self.names(original, 'PCBLIB')), 2)
        incoming = export_pcblib(self.data['C8734'], 'C8734')
        result, skipped = append_library(original, incoming, 'PCBLIB')
        self.assertEqual(result, original)
        self.assertEqual(skipped, ['Old_LQFP_A'])

    def test_standalone_files_do_not_gain_part_numbers(self):
        row = backend.download_batch(['C23922'], backend.Options(self.root, ('SCHLIB', 'PCBLIB')), api=self.api)[0]
        self.assertEqual(row.status, '成功')
        self.assertEqual(Path(row.folder).name, 'STM32F030C8T6_C23922')
        self.assertEqual({Path(file).name for file in row.files}, {'STM32F030C8T6.SchLib', 'STM32F030C8T6.PcbLib'})
        for file in row.files:
            self.assertTrue(all('C23922' not in name for name in self.names(file, Path(file).suffix[1:].upper())))

    def test_cancelled_shared_export_does_not_publish_pending_independent_pairs(self):
        options = backend.Options(self.root / 'out', ('SCHLIB', 'PCBLIB'), True, True,
                                  keep_schlib=True, keep_pcblib=True)
        def stop(index, row):
            if row.pending_formats:
                self.api.cancelled.set()
        rows = backend.download_batch(['C23922', 'C8734'], options, self.api.cancelled, api=self.api,
                                      on_result=stop, workers=1)
        self.assertTrue(all(row.status == '已取消' for row in rows))
        self.assertFalse(list((self.root / 'out').rglob('*.SchLib')))
        self.assertFalse(list((self.root / 'out').rglob('*.PcbLib')))

    def test_individual_write_failure_still_reports_partial_after_successful_shared_export(self):
        options = backend.Options(self.root / 'out', ('SCHLIB', 'PCBLIB'), True, True,
                                  keep_schlib=True, keep_pcblib=True)
        original_write = backend.atomic_write
        def write(path, payload):
            if path.parent.name.endswith('_C8734') and path.suffix == '.SchLib':
                raise PermissionError('locked')
            original_write(path, payload)
        with patch('backend.atomic_write', side_effect=write):
            rows = backend.download_batch(['C23922', 'C8734'], options, api=self.api)
        self.assertEqual([row.status for row in rows], ['成功', '部分完成'])
        self.assertEqual(len(self.names(options.merged_paths()['PCBLIB'], 'PCBLIB')), 1)
        self.assertIn('SCHLIB', rows[1].message)


if __name__ == '__main__':
    unittest.main()
