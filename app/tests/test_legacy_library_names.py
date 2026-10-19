"""Append native ANSI/Unicode library indices without changing existing data."""
import copy
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import olefile
from lcsc3d.core import app_logging as logs
from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.altium import MergedLibrary, _block, _parameters
from lcsc3d.eda.compound_storage import compound_file
from lcsc3d.eda.library_merge import (
    LibraryIndex, Reader, append_library, patch_parameters, read_library,
    rewrite_symbol_records, parameters, name_key,
)


def native_string(value):
    return struct.pack('<I', len(value) + 1) + bytes([len(value)]) + value


def native_parameters(values):
    """Frame native ANSI fields, including GBK trail bytes equal to '|'."""
    fields = []
    for key, value in values.items():
        try:
            raw = str(value).encode('cp1252', errors='surrogateescape')
        except UnicodeEncodeError:
            key, raw = '%UTF8%' + key, str(value).encode('utf-8')
        fields.append(b'|' + key.encode('ascii') + b'=' + raw)
    return _block(b''.join(fields) + b'\0')


@unittest.skipUnless(sys.platform == 'win32', 'Native AD compound storage')
class LegacyLibraryNamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'profile')})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(logs.close_logging)
        self.data = {part: json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))
                     for part in ('C2040', 'C20197')}

    def library(self, fmt, part):
        library = MergedLibrary(fmt, merge_pcb=True)
        library.add(self.data[part], part, lambda: None)
        return library.build([part], lambda: None)

    def native_library(self, fmt, name, encoding, *, mapped=False, binary=False, section=None):
        payload = self.library(fmt, 'C20197')
        index = LibraryIndex(payload, fmt)
        try:
            streams = {'/'.join(path): index.read(path) for path in index.ole.listdir()}
            source = index.sections[name_key(index.names[0])]
            header = dict(index.values) if fmt == 'SCHLIB' else index.header
        finally:
            index.ole.close()
        section = section or re.sub(r'[/\\:!*?]', '_', name)[:31]
        for path in list(streams):
            if path.startswith(source + '/'):
                streams[section + path[len(source):]] = streams.pop(path)
        raw = name.encode(encoding)
        byte_name = raw.decode('cp1252', errors='surrogateescape')
        key_bytes = section.encode(encoding)
        key_name = key_bytes.decode('cp1252', errors='surrogateescape')
        if fmt == 'SCHLIB':
            header['LIBREF0'] = byte_name
            streams['FileHeader'] = native_parameters(header)
            if binary:
                streams['FileHeader'] += struct.pack('<I', 1) + native_string(raw)
            data = rewrite_symbol_records(streams[section + '/Data'], name='LegacyNamePlaceholder')
            reader = Reader(data)
            flag, body = reader.block()
            streams[section + '/Data'] = (_block(body.replace(b'LegacyNamePlaceholder', raw), flag)
                                          + data[reader.offset:])
            streams['SectionKeys'] = native_parameters({'KeyCount': 1, 'LibRef0': byte_name,
                                                  'SectionKey0': key_name, 'VendorField': 'retained'})
        else:
            streams['Library/Data'] = struct.pack('<I', len(header)) + header + struct.pack('<I', 1) + native_string(raw)
            reader = Reader(streams[section + '/Data'])
            reader.string()
            streams[section + '/Data'] = native_string(raw) + reader.data[reader.offset:]
            reader = Reader(streams[section + '/Parameters'])
            parameters_block = patch_parameters(reader.block()[1], {'PATTERN': 'LegacyNamePlaceholder'})
            streams[section + '/Parameters'] = _block(
                Reader(parameters_block).block()[1].replace(b'LegacyNamePlaceholder', raw))
            streams['SectionKeys'] = struct.pack('<I', 1) + native_string(raw) + native_string(key_bytes)
        if not mapped:
            streams.pop('SectionKeys', None)
        streams['Vendor/Private'] = b'opaque vendor data'
        return compound_file(streams), section, byte_name

    def assert_old_streams_preserved(self, original, result, section):
        with olefile.OleFileIO(io.BytesIO(original)) as before, olefile.OleFileIO(io.BytesIO(result)) as after:
            for path in before.listdir():
                if path[0] in (section, 'Vendor') or path[:2] == ['Library', 'Models']:
                    self.assertEqual(before.openstream(path).read(), after.openstream(path).read(), path)

    def test_legacy_and_western_indices_append_and_reopen_without_data_changes(self):
        for fmt in ('SCHLIB', 'PCBLIB'):
            for encoding, name in [('gbk', '底壳丂_0603_Ω±1%*?'),
                                   ('utf-8', '中文符号_Ω±1%*?'),
                                   ('cp1252', 'Café ±1%*?')]:
                for mapped in (False, True):
                    with self.subTest(fmt=fmt, encoding=encoding, mapped=mapped):
                        original, section, byte_name = self.native_library(fmt, name, encoding, mapped=mapped)
                        result, skipped = append_library(original, self.library(fmt, 'C2040'), fmt)
                        self.assertEqual(skipped, [])
                        index = LibraryIndex(result, fmt)
                        try:
                            self.assertEqual(len(index.names), 2)
                            self.assertEqual(index.names[0].encode('cp1252', errors='surrogateescape'), name.encode(encoding))
                            self.assertEqual(index.sections[name_key(index.names[0])], section)
                        finally:
                            index.ole.close()
                        self.assert_old_streams_preserved(original, result, section)
                        repeated, _ = append_library(result, self.library(fmt, 'C2040'), fmt)
                        self.assertEqual(repeated, result)

    def test_native_binary_schematic_index_keeps_existing_name_bytes(self):
        original, section, _ = self.native_library('SCHLIB', '电阻丂Ω±*?', 'gbk', binary=True)
        result, _ = append_library(original, self.library('SCHLIB', 'C2040'), 'SCHLIB')
        with olefile.OleFileIO(io.BytesIO(result)) as ole:
            reader = Reader(ole.openstream('FileHeader').read())
            reader.block()
            self.assertEqual(reader.uint(), 2)
            start = reader.offset
            reader.string()
            self.assertEqual(reader.data[start:reader.offset], native_string('电阻丂Ω±*?'.encode('gbk')))
        self.assert_old_streams_preserved(original, result, section)

    def test_explicit_alias_and_long_names_use_native_section_keys(self):
        for fmt in ('SCHLIB', 'PCBLIB'):
            original, section, name = self.native_library(fmt, '功率电感_型号丂_' + 'A' * 40, 'gbk',
                                                         mapped=True, section='中文别名丂')
            result, _ = append_library(original, self.library(fmt, 'C2040'), fmt)
            index = LibraryIndex(result, fmt)
            try:
                self.assertEqual(index.sections[name_key(index.names[0])], section)
            finally:
                index.ole.close()
            self.assert_old_streams_preserved(original, result, section)

    def test_reusing_legacy_footprint_preserves_original_and_native_reference(self):
        original, section, byte_name = self.native_library('PCBLIB', '底壳丂Ω±', 'gbk')
        references = {}
        # Same geometry under a native name must reuse the old component.
        result, _ = append_library(original, self.library('PCBLIB', 'C20197'), 'PCBLIB',
                                   footprint_names=references)
        self.assertEqual(result, original)
        self.assertIn(byte_name, references.values())
        merged = MergedLibrary('PCBLIB', merge_pcb=True)
        merged.add(self.data['C20197'], 'C20197', lambda: None)
        pending = merged.build(['C20197'], lambda: None, original=original)
        self.assertEqual(merged.footprint_names['C20197'].encode('cp1252', errors='surrogateescape'),
                         '底壳丂Ω±'.encode('gbk'))
        result, _ = append_library(original, pending, 'PCBLIB')
        self.assertEqual(result, original)

    def test_ambiguous_encoding_is_rejected(self):
        name = '中文'
        raw = name.encode('utf-8')
        other = raw.decode('gb18030')
        original, section, _ = self.native_library('SCHLIB', name, 'utf-8')
        original = compound_file({other + '/Data': b'other component'}, original=original)
        with self.assertRaisesRegex(DownloadError, '多个条目'):
            LibraryIndex(original, 'SCHLIB')

    def test_missing_explicit_section_is_rejected_even_if_name_exists(self):
        original, _, byte_name = self.native_library('SCHLIB', '电阻', 'gbk')
        original = compound_file({'SectionKeys': _parameters({'KeyCount': 1, 'LibRef0': byte_name,
                                                              'SectionKey0': 'missing'})}, original=original)
        with self.assertRaisesRegex(DownloadError, '不存在'):
            LibraryIndex(original, 'SCHLIB')

    def test_previous_exports_with_literal_special_characters_still_append(self):
        for fmt in ('SCHLIB', 'PCBLIB'):
            original, section, _ = self.native_library(fmt, 'DIP*2?', 'cp1252', section='DIP*2?')
            result, _ = append_library(original, self.library(fmt, 'C2040'), fmt)
            self.assert_old_streams_preserved(original, result, section)

    def test_append_does_not_casefold_legacy_bytes_into_an_unrelated_name(self):
        from lcsc3d.services import backend
        for fmt in ('SCHLIB', 'PCBLIB'):
            for section_alias in (None, '逜', 'OpaqueAlias'):
                with self.subTest(fmt=fmt, alias=section_alias):
                    original, section, _ = self.native_library(fmt, '逜', 'gbk',
                        mapped=section_alias is not None, section=section_alias)
                    target = self.root / ('target.' + fmt)
                    target.write_bytes(original)
                    data = copy.deepcopy(self.data['C2040'])
                    data['title'] = 'ssA'
                    data['packageDetail']['title'] = 'ssA'
                    api = backend.NetworkApi(use_cache=False)
                    api.get_cad_data_of_component = lambda part: copy.deepcopy(data)
                    options = backend.Options(self.root / 'export', (fmt,), append_mode=True,
                        **{fmt.lower() + '_target': str(target)}, bind_3d_models=False)
                    rows = backend.download_batch(['C2040'], options, api=api, workers=1)
                    self.assertEqual(rows[0].status, '成功', rows[0].message)
                    result = target.read_bytes()
                    index = LibraryIndex(result, fmt)
                    try:
                        self.assertEqual(len(index.names), 2)
                        self.assertIn('ssA', index.names)
                    finally:
                        index.ole.close()
                    self.assert_old_streams_preserved(original, result, section)
                    rows = backend.download_batch(['C2040'], options, api=api, workers=1)
                    self.assertEqual(rows[0].status, '成功', rows[0].message)
                    self.assertEqual(target.read_bytes(), result)

    def test_gbk_pipe_trail_bytes_survive_header_keys_and_repeated_append(self):
        for fmt in ('SCHLIB', 'PCBLIB'):
            for mapped in (False, True):
                for name in ('東', '東侧_1'):
                    with self.subTest(fmt=fmt, mapped=mapped, name=name):
                        original, section, _ = self.native_library(fmt, name, 'gbk', mapped=mapped)
                        result, skipped = append_library(original, self.library(fmt, 'C2040'), fmt)
                        self.assertEqual(skipped, [])
                        index = LibraryIndex(result, fmt)
                        try:
                            self.assertEqual(len(index.names), 2)
                            self.assertEqual(index.names[0].encode('cp1252', errors='surrogateescape'), name.encode('gbk'))
                            if fmt == 'SCHLIB':
                                self.assertIn(b'|LIBREF0=' + name.encode('gbk') + b'|', index.header)
                        finally:
                            index.ole.close()
                        self.assert_old_streams_preserved(original, result, section)
                        repeated, _ = append_library(result, self.library(fmt, 'C2040'), fmt)
                        self.assertEqual(repeated, result)

    def test_binary_gbk_schematic_with_pipe_trail_byte_preserves_native_header(self):
        original, section, _ = self.native_library('SCHLIB', '東', 'gbk', binary=True, mapped=True)
        result, _ = append_library(original, self.library('SCHLIB', 'C2040'), 'SCHLIB')
        index = LibraryIndex(result, 'SCHLIB')
        try:
            self.assertEqual(len(index.names), 2)
            self.assertTrue(index.raw_names.startswith(native_string('東'.encode('gbk'))))
            self.assertIn(b'|LIBREF0=' + '東'.encode('gbk') + b'|', index.header)
        finally:
            index.ole.close()
        self.assert_old_streams_preserved(original, result, section)

    def test_gbk_names_containing_parameter_like_suffixes_preserve_index_and_metadata(self):
        for binary in (False, True):
            for mapped in (False, True):
                for name in ('東A=B', '東Weight=4', '東KEYCOUNT=99999', '東FontIDCount=99999',
                             '東COMPCOUNT=0', '東KEYCOUNT=0'):
                    with self.subTest(binary=binary, mapped=mapped, name=name):
                        original, section, _ = self.native_library('SCHLIB', name, 'gbk', binary=binary, mapped=mapped)
                        with olefile.OleFileIO(io.BytesIO(original)) as ole:
                            original_header = Reader(ole.openstream('FileHeader').read()).block()[1]
                        weight = int(re.search(rb'\|WEIGHT=(\d+)', original_header)[1])
                        generated = self.library('SCHLIB', 'C2040')
                        new = LibraryIndex(generated, 'SCHLIB')
                        try:
                            added_weight = int(new.values['WEIGHT'])
                        finally:
                            new.ole.close()
                        result, skipped = append_library(original, generated, 'SCHLIB')
                        self.assertEqual(skipped, [])
                        index = LibraryIndex(result, 'SCHLIB')
                        try:
                            self.assertEqual(len(index.names), 2)
                            self.assertEqual(index.names[0].encode('cp1252', errors='surrogateescape'), name.encode('gbk'))
                            self.assertEqual(index.values['LIBREF0'].encode('cp1252', errors='surrogateescape'), name.encode('gbk'))
                            self.assertEqual(int(index.values['WEIGHT']), weight + added_weight)
                            self.assertIn(b'|LIBREF0=' + name.encode('gbk') + b'|', index.header)
                            if mapped:
                                self.assertIn(b'|LibRef0=' + name.encode('gbk') + b'|', index.key_header)
                        finally:
                            index.ole.close()
                        self.assert_old_streams_preserved(original, result, section)
                        repeated, _ = append_library(result, generated, 'SCHLIB')
                        self.assertEqual(repeated, result)

    def test_patching_header_preserves_unknown_fields_spelling_and_duplicate_bytes(self):
        payload = (b'|VendorUnknown|LIBREF0=' + '東'.encode('gbk') + b'|Weight=4'
                   b'|Vendor#Flag=first|Vendor#Flag=second|%UTF8%Notes=' + '中文'.encode('utf-8') + b'\0')
        updated = Reader(patch_parameters(payload, {'WEIGHT': 8})).block()[1]
        self.assertEqual(updated, payload.replace(b'|Weight=4', b'')[:-1] + b'|WEIGHT=8\0')
        fields = parameters(updated)
        self.assertEqual(fields['LIBREF0'].encode('cp1252'), '東'.encode('gbk'))
        self.assertEqual(fields['VENDOR#FLAG'], 'second')
        self.assertEqual(fields['NOTES'], '中文')
        self.assertEqual(fields['WEIGHT'], '8')

    def test_reused_legacy_footprint_keeps_standalone_outputs_and_symbol_links(self):
        from lcsc3d.services import backend
        for name in ('底壳丂Ω±', '東', '東A=B', '東Weight=4'):
            with self.subTest(name=name):
                original, _, byte_name = self.native_library('PCBLIB', name, 'gbk')
                target = self.root / 'existing.PcbLib'
                target.write_bytes(original)
                api = backend.NetworkApi(use_cache=False)
                api.get_cad_data_of_component = lambda part: copy.deepcopy(self.data[part])
                options = backend.Options(self.root / 'standalone', ('SCHLIB', 'PCBLIB'), True, True,
                    keep_schlib=True, keep_pcblib=True, pcblib_target=str(target), bind_3d_models=False)
                rows = backend.download_batch(['C20197'], options, api=api, workers=1)
                self.assertEqual(rows[0].status, '成功', rows[0].message)
                self.assertEqual(rows[0].export_errors, [])
                self.assertEqual(target.read_bytes(), original)
                standalone = [Path(file) for file in rows[0].files
                              if Path(file).suffix.lower() == '.pcblib' and Path(file) != target]
                self.assertEqual(len(standalone), 1)
                index = LibraryIndex(standalone[0].read_bytes(), 'PCBLIB')
                try:
                    self.assertEqual(index.names, [byte_name])
                    self.assertEqual(index.raw_names, native_string(name.encode('gbk')))
                finally:
                    index.ole.close()
                symbols = [Path(file) for file in rows[0].files if Path(file).suffix.lower() == '.schlib']
                self.assertEqual(len(symbols), 2)
                for symbol in symbols:
                    with olefile.OleFileIO(str(symbol)) as ole:
                        path = next(path for path in ole.listdir() if path[-1] == 'Data')
                        reader = Reader(ole.openstream(path).read())
                        references = []
                        while reader.offset < len(reader.data):
                            flag, body = reader.block()
                            if not flag:
                                fields = parameters(body, native_values={b'MODELNAME': name.encode('gbk'),
                                                                         b'TEXT': name.encode('gbk')})
                                if fields.get('RECORD') == '45':
                                    references.append(fields['MODELNAME'])
                                if fields.get('RECORD') == '41' and fields.get('NAME') == 'Footprint':
                                    references.append(fields['TEXT'])
                        self.assertEqual(references, [byte_name, byte_name])

    def test_diagnostics_record_compatibility_and_failure_without_private_names(self):
        folder = self.root / 'logs'
        logs.configure_logging(directory=folder, version='2.4.4')
        private_name = '私有器件丂Ω±'
        payload, _, byte_name = self.native_library('SCHLIB', private_name, 'gbk')
        path = self.root / 'private.SchLib'
        path.write_bytes(payload)
        self.assertEqual(read_library(path, 'SCHLIB'), payload)
        broken = compound_file({'SectionKeys': _parameters({'KeyCount': 1, 'LibRef0': byte_name,
                                                            'SectionKey0': 'missing'})}, original=payload)
        path.write_bytes(broken)
        with self.assertRaises(DownloadError):
            read_library(path, 'SCHLIB')
        text = (folder / 'LCSC3D.log').read_text(encoding='utf-8')
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(any(row['event'] == 'library.index_resolved' and row['legacy_name_count'] == 1 for row in rows))
        failure = next(row for row in rows if row['event'] == 'library.read_existing.failed')
        self.assertTrue(failure['error_id'])
        self.assertTrue(failure['causes'][0]['frames'])
        self.assertNotIn(private_name, text)
        self.assertNotIn('private.SchLib', text)


if __name__ == '__main__':
    unittest.main()
