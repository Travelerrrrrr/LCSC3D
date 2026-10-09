"""User-owned library and project files survive incremental export."""
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
from altium import MergedLibrary, _block, _parameters, _string_block
from compound_storage import compound_file
from library_merge import append_library, LibraryIndex, Reader, parameters
from altium_project import add_libraries_to_project, commit_user_file
from app_logging import close_logging
from errors import DownloadError, Cancelled
from altium_inspect import schematic, pcb, merged_pcb_section
import olefile


@unittest.skipUnless(sys.platform == 'win32', 'Native AD libraries')
class LibraryIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'profile')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)
        self.data = {part: json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))
                     for part in ('C2040', 'C20197', 'C2765186')}

    def library(self, format, part):
        lib = MergedLibrary(format, merge_pcb=True)
        lib.add(self.data[part], part, lambda: None)
        return lib.build([part], lambda: None)

    def test_append_preserves_old_components_models_and_unknown_streams(self):
        for format in ('SCHLIB', 'PCBLIB'):
            with self.subTest(format=format):
                original = self.library(format, 'C2040')
                original = compound_file({'Vendor/Private': b'opaque payload', 'Library/Models/Custom': bytes(range(256))}, original=original)
                added = self.library(format, 'C20197')
                result, skipped = append_library(original, added, format)
                self.assertEqual(skipped, [])
                old = LibraryIndex(original, format)
                new = LibraryIndex(result, format)
                try:
                    self.assertEqual(len(new.names), 2)
                    self.assertEqual(new.read('Vendor/Private'), b'opaque payload')
                    self.assertEqual(new.read('Library/Models/Custom'), bytes(range(256)))
                    section = old.sections[old.names[0].casefold()]
                    for path in old.ole.listdir():
                        if path[0] == section:
                            self.assertEqual(old.read(path), new.read(path))
                finally:
                    old.ole.close(); new.ole.close()

    def test_new_symbol_fonts_and_pins_remain_correct_after_existing_font_table(self):
        original = self.library('SCHLIB', 'C2040')
        incoming = self.library('SCHLIB', 'C2765186')
        merged, _ = append_library(original, incoming, 'SCHLIB')
        header, rows, pins = schematic(merged, 'C2765186_Symbol')
        source_header, source_rows, source_pins = schematic(incoming, 'C2765186_Symbol')
        self.assertEqual(pins, source_pins)
        for row, source in zip(rows, source_rows):
            for key, value in row.items():
                if key.endswith('FONTID'):
                    self.assertEqual(header['FONTNAME' + value], source_header['FONTNAME' + source[key]])
                    self.assertEqual(header['SIZE' + value], source_header['SIZE' + source[key]])

    def test_duplicate_names_preserve_exact_original_bytes(self):
        for format in ('SCHLIB', 'PCBLIB'):
            original = self.library(format, 'C2040')
            result, skipped = append_library(original, self.library(format, 'C2040'), format)
            self.assertEqual(result, original)
            self.assertEqual(len(skipped), 1)

    def test_skipped_symbols_do_not_inflate_record_count_or_discard_index_metadata(self):
        original = self.library('SCHLIB', 'C2040')
        index = LibraryIndex(original, 'SCHLIB')
        weight = int(index.values['WEIGHT'])
        key_fields = parameters(index.key_header)
        index.ole.close()
        key_fields['CUSTOM_INDEX_FIELD'] = 'preserved'
        original = compound_file({'SectionKeys': _parameters(key_fields)}, original=original)
        incoming = MergedLibrary('SCHLIB')
        for part in ('C2040', 'C20197'):
            incoming.add(self.data[part], part, lambda: None)
        addition = self.library('SCHLIB', 'C20197')
        index = LibraryIndex(addition, 'SCHLIB')
        weight += int(index.values['WEIGHT'])
        index.ole.close()
        result, skipped = append_library(original, incoming.build(['C2040', 'C20197'], lambda: None), 'SCHLIB')
        index = LibraryIndex(result, 'SCHLIB')
        try:
            self.assertEqual(len(skipped), 1)
            self.assertEqual(int(index.values['WEIGHT']), weight)
            self.assertEqual(parameters(index.key_header)['CUSTOM_INDEX_FIELD'], 'preserved')
        finally:
            index.ole.close()

    def test_binary_schematic_index_is_extended_without_losing_header_fields(self):
        original = self.library('SCHLIB', 'C2040')
        index = LibraryIndex(original, 'SCHLIB')
        names = index.names[:]
        raw = index.read('FileHeader')
        index.ole.close()
        original = compound_file({'FileHeader': raw + struct.pack('<I', 1) + _string_block(names[0])}, original=original)
        result, _ = append_library(original, self.library('SCHLIB', 'C20197'), 'SCHLIB')
        index = LibraryIndex(result, 'SCHLIB')
        try:
            self.assertTrue(index.binary_index)
            self.assertEqual(index.names[0], names[0])
            self.assertEqual(len(index.names), 2)
        finally:
            index.ole.close()

    def test_pcb_parameter_table_retains_existing_rows_and_lists_new_pads(self):
        original = self.library('PCBLIB', 'C2040')
        table = b'Name=original|Pad Count=57|Custom=preserved\r\n'
        original = compound_file({'Library/ComponentParamsTOC/Header': struct.pack('<I', 1),
                                  'Library/ComponentParamsTOC/Data': _block(table + b'\0')}, original=original)
        result, _ = append_library(original, self.library('PCBLIB', 'C20197'), 'PCBLIB')
        index = LibraryIndex(result, 'PCBLIB')
        try:
            body = Reader(index.read('Library/ComponentParamsTOC/Data')).block()[1]
            self.assertTrue(body.startswith(table))
            self.assertIn(b'|Pad Count=8|', body)
        finally:
            index.ole.close()

    def test_cancelled_append_and_malformed_libraries_never_modify_input(self):
        original = self.library('PCBLIB', 'C2040')
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            append_library(original, self.library('PCBLIB', 'C20197'), 'PCBLIB', cancel)
        with self.assertRaises(DownloadError):
            append_library(b'not a library', original, 'PCBLIB')

    def test_batch_keeps_individual_files_appends_selected_parts_and_registers_project(self):
        files = {}
        for format, extension in (('SCHLIB', 'SchLib'), ('PCBLIB', 'PcbLib')):
            files[format] = self.root / ('existing.' + extension)
            files[format].write_bytes(self.library(format, 'C20197'))
        project = self.root / 'Project.PrjPcb'
        original = b'[Design]\r\nVersion=1.0\r\nCustom=kept\r\n'
        project.write_bytes(original)
        api = backend.NetworkApi()
        api.get_cad_data_of_component = lambda part: self.data[part]
        options = backend.Options(self.root/'out', ('SCHLIB', 'PCBLIB'), True, True,
                                  keep_schlib=True, keep_pcblib=True, schlib_target=str(files['SCHLIB']),
                                  pcblib_target=str(files['PCBLIB']), project_path=str(project))
        results = backend.download_batch(['C2040', 'C2765186'], options, api=api)
        self.assertEqual([r.status for r in results], ['成功', '成功'])
        self.assertTrue(all(len(r.files) == 4 for r in results))
        for result in results:
            sch = next(Path(file) for file in result.files if file.endswith('.SchLib') and Path(file).parent != self.root)
            board = next(Path(file) for file in result.files if file.endswith('.PcbLib') and Path(file).parent != self.root)
            _, records, _ = schematic(sch.read_bytes())
            name, _, _ = pcb(board.read_bytes())
            self.assertEqual(next(row['MODELNAME'] for row in records if row.get('RECORD') == '45'), name)
        self.assertIn('库已加入 PCB 工程', results[0].message)
        for format, file in files.items():
            index = LibraryIndex(file.read_bytes(), format)
            self.assertEqual(len(index.names), 3)
            index.ole.close()
        self.assertTrue(project.read_bytes().startswith(original))
        self.assertEqual(project.read_text().count('DocumentPath='), 2)
        self.assertEqual(len(list((self.root/'profile/LCSC3D/backups').iterdir())), 3)
        self.assertFalse(list(self.root.glob('*.bak')))

    def test_individual_output_failure_is_not_hidden_by_successful_merge(self):
        api = backend.NetworkApi()
        api.get_cad_data_of_component = lambda part: self.data[part]
        options = backend.Options(self.root/'out', ('SCHLIB',), True, keep_schlib=True)
        original_write = backend.atomic_write
        def write(path, payload):
            if path.parent.name.endswith('C2040'):
                raise PermissionError('locked')
            original_write(path, payload)
        with patch('backend.atomic_write', side_effect=write):
            result = backend.download_batch(['C2040'], options, api=api)[0]
        self.assertEqual(result.status, '部分完成')
        self.assertEqual(len(result.files), 1)

    def test_existing_target_cannot_be_overwritten_by_individual_output(self):
        target = self.root/'RP2040_C2040/RP2040.SchLib'
        target.parent.mkdir()
        original = self.library('SCHLIB', 'C20197')
        target.write_bytes(original)
        api = backend.NetworkApi()
        api.get_cad_data_of_component = lambda part: self.data[part]
        options = backend.Options(self.root, ('SCHLIB',), True, keep_schlib=True, schlib_target=str(target))
        result = backend.download_batch(['C2040'], options, api=api)[0]
        self.assertEqual(result.status, '失败')
        self.assertIn('路径相同', result.message)
        self.assertEqual(target.read_bytes(), original)

    def test_project_duplicate_detection_preserves_encoding_and_existing_bytes(self):
        library = self.root / '中文.SchLib'
        library.write_bytes(self.library('SCHLIB', 'C2040'))
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'gb18030'):
            with self.subTest(encoding=encoding):
                project = self.root / (encoding + '.PrjPcb')
                original = '[Design]\r\nName=测试工程\r\n[Document3]\r\nDocumentPath=old.PcbDoc\r\n'.encode(encoding)
                project.write_bytes(original)
                result = add_libraries_to_project(project, [library])
                self.assertEqual(result['added'], 1)
                changed = project.read_bytes()
                self.assertTrue(changed.startswith(original))
                self.assertIn('[Document4]', changed.decode(encoding))
                self.assertEqual(Path(result['backup']).read_bytes(), original)
                result = add_libraries_to_project(project, [library])
                self.assertEqual((result['added'], result['skipped']), (0, 1))
                self.assertEqual(project.read_bytes(), changed)

    def test_concurrent_modification_and_project_write_failure_preserve_user_files(self):
        target = self.root / 'Project.PrjPcb'
        target.write_bytes(b'[Design]\nNew=user change\n')
        with self.assertRaises(DownloadError):
            commit_user_file(target, b'[Design]\n', b'replacement')
        self.assertEqual(target.read_bytes(), b'[Design]\nNew=user change\n')
        lib = self.root/'library.SchLib'
        lib.write_bytes(self.library('SCHLIB', 'C2040'))
        original_write = backend.atomic_write
        def write(path, payload):
            if path == target:
                raise PermissionError('locked')
            original_write(path, payload)
        with patch('backend.atomic_write', side_effect=write), self.assertRaises(PermissionError):
            add_libraries_to_project(target, [lib])
        self.assertEqual(target.read_bytes(), b'[Design]\nNew=user change\n')


if __name__ == '__main__':
    unittest.main()
