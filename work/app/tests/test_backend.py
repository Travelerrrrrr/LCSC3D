import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import Cancelled, DownloadError, NetworkApi, Options, atomic_write, download_part, parse_part_numbers, safe_filename
from altium import LibraryExport, OLE_MAGIC

STEP = b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'


class FakeApi:
    def __init__(self, step=STEP, obj=None):
        self.step, self.obj = step, obj
        self.step_calls = 0

    def check_cancelled(self):
        pass

    def get_cad_data_of_component(self, part):
        return {'title': 'Test component', 'lcsc': {'id': 2392}}

    def get_step_3d_model(self, uuid):
        self.step_calls += 1
        if isinstance(self.step, Exception):
            raise self.step
        return self.step

    def get_raw_3d_model_obj(self, uuid):
        return self.obj


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.model = SimpleNamespace(name='QFN/56', uuid='test-uuid')

    def tearDown(self):
        self.temp.cleanup()

    def importer(self):
        return patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=self.model))

    def test_input_dedup_and_invalid_number(self):
        self.assertEqual(parse_part_numbers('c2040，C20197\nC2040; C12bad'), (['C2040', 'C20197'], ['C12bad'], 1))

    def test_step_survives_unavailable_obj_and_wrl(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder, ('STEP', 'WRL')), FakeApi())
        self.assertEqual(result.status, '部分完成')
        self.assertEqual((self.folder / 'C2040/C2040_QFN_56.step').read_bytes(), STEP)
        self.assertFalse((self.folder / 'C2040/C2040_QFN_56.wrl').exists())

    def test_rerun_preserves_existing_file_without_download(self):
        with self.importer():
            download_part('C2040', Options(self.folder), FakeApi())
            api = FakeApi(step=DownloadError('should not be called'))
            result = download_part('C2040', Options(self.folder), api)
        self.assertEqual(result.status, '已存在')
        self.assertEqual(api.step_calls, 0)

    def test_network_failure_has_no_placeholder_file(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi(step=DownloadError('timeout')))
        self.assertEqual(result.status, '失败')
        self.assertFalse((self.folder / 'C2040/C2040_QFN_56.step').exists())

    def test_zero_byte_output_is_replaced(self):
        path = self.folder / 'C2040/C2040_QFN_56.step'
        path.parent.mkdir()
        path.touch()
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi())
        self.assertEqual(result.status, '成功')
        self.assertEqual(path.read_bytes(), STEP)

    def test_different_part_numbers_do_not_share_a_file(self):
        with self.importer():
            download_part('C2040', Options(self.folder), FakeApi())
            download_part('C20197', Options(self.folder), FakeApi())
        self.assertEqual(len(list(self.folder.rglob('*.step'))), 2)

    def test_cancelled_request_does_not_connect(self):
        event = threading.Event()
        event.set()
        api = NetworkApi(event)
        with patch('urllib.request.urlopen') as connect:
            with self.assertRaises(Cancelled):
                api.fetch('https://easyeda.com/')
            connect.assert_not_called()

    def test_filename_sanitizing_keeps_output_inside_target(self):
        self.assertNotIn('/', safe_filename('../../CON:<bad>'))
        self.assertEqual(safe_filename('CON'), '_CON')

    def test_libraries_export_without_a_3d_model(self):
        exported = LibraryExport({'SCHLIB': OLE_MAGIC + b'x' * 512, 'PCBLIB': OLE_MAGIC + b'y' * 512})
        with patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=None)), \
                patch('backend.export_libraries', return_value=exported):
            result = download_part('C2040', Options(self.folder, ('SCHLIB', 'PCBLIB')), FakeApi())
        self.assertEqual(result.status, '成功')
        self.assertEqual({Path(file).suffix for file in result.files}, {'.SchLib', '.PcbLib'})
        metadata = json.loads((self.folder / 'C2040/model-info.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['model_uuid'], '')
        self.assertIn('altium', metadata)

    def test_missing_model_is_partial_only_if_model_format_requested(self):
        exported = LibraryExport({'SCHLIB': OLE_MAGIC + b'x' * 512})
        with patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=None)), \
                patch('backend.export_libraries', return_value=exported):
            result = download_part('C2040', Options(self.folder, ('STEP', 'SCHLIB')), FakeApi())
        self.assertEqual(result.status, '部分完成')
        self.assertTrue((self.folder / 'C2040/C2040.SchLib').exists())

    def test_converter_failure_preserves_saved_step(self):
        with self.importer(), patch('backend.export_libraries', side_effect=DownloadError('converter failed')):
            result = download_part('C2040', Options(self.folder, ('STEP', 'SCHLIB')), FakeApi())
        self.assertEqual(result.status, '部分完成')
        self.assertEqual((self.folder / 'C2040/C2040_QFN_56.step').read_bytes(), STEP)
        self.assertFalse((self.folder / 'C2040/C2040.SchLib').exists())

    def test_symbol_success_survives_footprint_conversion_failure(self):
        exported = LibraryExport({'SCHLIB': OLE_MAGIC + b'x' * 512}, {'PCBLIB': 'bad footprint'})
        with self.importer(), patch('backend.export_libraries', return_value=exported):
            result = download_part('C2040', Options(self.folder, ('SCHLIB', 'PCBLIB')), FakeApi())
        self.assertEqual(result.status, '部分完成')
        self.assertEqual(len(result.files), 1)
        self.assertIn('bad footprint', result.message)

    def test_existing_libraries_are_not_converted_or_overwritten(self):
        folder = self.folder / 'C2040'
        folder.mkdir()
        for extension in ('SchLib', 'PcbLib'):
            (folder / f'C2040.{extension}').write_bytes(b'original')
        with self.importer(), patch('backend.export_libraries') as convert:
            result = download_part('C2040', Options(self.folder, ('SCHLIB', 'PCBLIB')), FakeApi())
        self.assertEqual(result.status, '已存在')
        convert.assert_not_called()
        self.assertEqual((folder / 'C2040.PcbLib').read_bytes(), b'original')

    def test_step_is_fetched_once_for_download_and_embedded_footprint(self):
        api = FakeApi()
        exported = LibraryExport({'PCBLIB': OLE_MAGIC + b'x' * 512}, details={'footprint': {'step_embedded': True}})
        with self.importer(), patch('backend.export_libraries', return_value=exported) as convert:
            result = download_part('C2040', Options(self.folder, ('STEP', 'PCBLIB')), api)
        self.assertEqual(api.step_calls, 1)
        self.assertEqual(convert.call_args.args[-1], STEP)
        self.assertEqual(result.status, '成功')

    def test_unavailable_optional_step_does_not_block_2d_footprint(self):
        exported = LibraryExport({'PCBLIB': OLE_MAGIC + b'x' * 512}, details={'footprint': {'step_embedded': False}})
        with self.importer(), patch('backend.export_libraries', return_value=exported):
            result = download_part('C2040', Options(self.folder, ('PCBLIB',)), FakeApi(step=DownloadError('timeout')))
        self.assertEqual(result.status, '成功')
        self.assertIn('未嵌入 STEP', result.message)
        self.assertIn('timeout', result.message)

    def test_cancelled_converter_creates_no_library(self):
        with self.importer(), patch('backend.export_libraries', side_effect=Cancelled):
            with self.assertRaises(Cancelled):
                download_part('C2040', Options(self.folder, ('SCHLIB',)), FakeApi())
        self.assertFalse((self.folder / 'C2040/C2040.SchLib').exists())


if __name__ == '__main__':
    unittest.main()
