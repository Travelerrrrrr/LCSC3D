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

STEP = b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'


class FakeApi:
    def __init__(self, step=STEP, obj=None, title='Test component'):
        self.step, self.obj = step, obj
        self.title = title
        self.step_calls = 0

    def check_cancelled(self):
        pass

    def get_cad_data_of_component(self, part):
        return {'title': self.title, 'lcsc': {'id': 2392}}

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
        self.assertEqual((self.folder / 'Test component_C2040/C2040_QFN_56.step').read_bytes(), STEP)
        self.assertFalse((self.folder / 'Test component_C2040/C2040_QFN_56.wrl').exists())

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
        self.assertFalse((self.folder / 'Test component_C2040/C2040_QFN_56.step').exists())

    def test_zero_byte_output_is_replaced(self):
        path = self.folder / 'Test component_C2040/C2040_QFN_56.step'
        path.parent.mkdir()
        path.touch()
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi())
        self.assertEqual(result.status, '成功')
        self.assertEqual(path.read_bytes(), STEP)

    def test_different_part_numbers_do_not_share_a_file(self):
        with self.importer():
            first = download_part('C2040', Options(self.folder), FakeApi())
            second = download_part('C20197', Options(self.folder), FakeApi())
        self.assertEqual(Path(first.folder).name, 'Test component_C2040')
        self.assertEqual(Path(second.folder).name, 'Test component_C20197')
        self.assertEqual(len(list(self.folder.rglob('*.step'))), 2)

    def test_folder_uses_component_title_instead_of_model_name(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi(title='RP2040'))
        self.assertEqual(Path(result.folder), self.folder / 'RP2040_C2040')
        self.assertEqual(result.status, '成功')
        self.assertTrue(all(Path(file).parent == Path(result.folder) for file in result.files))

    def test_folder_handles_unicode_and_windows_characters(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi(title=' 温度/传感器:V1 '))
        self.assertEqual(Path(result.folder), self.folder / '温度_传感器_V1_C2040')
        self.assertEqual(result.status, '成功')
        metadata = json.loads((Path(result.folder) / 'model-info.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['title'], '温度/传感器:V1')

    def test_missing_title_still_exports_to_number_suffixed_folder(self):
        for data in ({}, {'title': None}, {'title': ''}, {'title': ' \t '}):
            with self.subTest(data=data):
                api = FakeApi()
                with self.importer(), patch.object(api, 'get_cad_data_of_component', return_value=data):
                    result = download_part('C2040', Options(self.folder, overwrite=True), api)
                self.assertEqual(Path(result.folder), self.folder / '未命名器件_C2040')
                self.assertEqual(result.status, '成功')

    def test_long_title_keeps_number_suffix_and_stays_inside_destination(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder), FakeApi(title='../' + 'x' * 200))
        folder = Path(result.folder)
        self.assertEqual(folder.resolve().parent, self.folder.resolve())
        self.assertTrue(folder.name.endswith('_C2040'))
        self.assertLessEqual(len(folder.name), 122)
        self.assertEqual(result.status, '成功')

    def test_cancelled_request_does_not_connect(self):
        event = threading.Event()
        event.set()
        api = NetworkApi(event)
        with patch('urllib.request.urlopen') as connect:
            with self.assertRaises(Cancelled):
                api.fetch('https://easyeda.com/')
            connect.assert_not_called()

    def test_official_svg_endpoint_records_domestic_source(self):
        payload = b'{"success":true,"result":[]}'
        api = NetworkApi()
        with patch.object(api, 'fetch', return_value=payload) as fetch:
            data = api.get_svg_data_of_component('C2040')
        fetch.assert_called_once_with('https://lceda.cn/api/products/C2040/svgs')
        self.assertEqual(data['source_url'], 'https://lceda.cn/api/products/C2040/svgs')
        self.assertEqual(data['result'], [])

    def test_official_svg_mirror_recovers_host_failure(self):
        api = NetworkApi()
        with patch.object(api, 'fetch', side_effect=[DownloadError('HTTP 418'), b'{"success":true,"result":[]}']) as fetch:
            data = api.get_svg_data_of_component('C20197')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(data['source_url'], 'https://easyeda.com/api/products/C20197/svgs')

    def test_svg_error_and_cancellation_never_return_placeholder_data(self):
        api = NetworkApi()
        for response in (b'<html>blocked</html>', b'{"success":false,"result":[]}', b'{"success":true,"result":{}}'):
            with self.subTest(response=response), patch.object(api, 'fetch', return_value=response) as fetch:
                with self.assertRaises(DownloadError):
                    api.get_svg_data_of_component('C2040')
                self.assertEqual(fetch.call_count, 2)
        with patch.object(api, 'fetch', side_effect=Cancelled()) as fetch:
            with self.assertRaises(Cancelled):
                api.get_svg_data_of_component('C2040')
            fetch.assert_called_once()

    def test_svg_invalid_number_never_connects(self):
        api = NetworkApi()
        with patch.object(api, 'fetch') as fetch:
            with self.assertRaises(DownloadError):
                api.get_svg_data_of_component('../C2040')
            fetch.assert_not_called()

    def test_filename_sanitizing_keeps_output_inside_target(self):
        self.assertNotIn('/', safe_filename('../../CON:<bad>'))
        self.assertEqual(safe_filename('CON'), '_CON')

    def test_removed_library_formats_are_rejected_before_network_access(self):
        for formats in (('SCHLIB',), ('PCBLIB',), ('STEP', 'SCHLIB'), ()):
            with self.subTest(formats=formats), patch.object(FakeApi, 'get_cad_data_of_component') as fetch:
                with self.assertRaises(DownloadError):
                    download_part('C2040', Options(self.folder, formats), FakeApi())
                fetch.assert_not_called()

    def test_missing_model_does_not_create_output_files(self):
        with patch('backend.Easyeda3dModelImporter', return_value=SimpleNamespace(output=None)):
            result = download_part('C2040', Options(self.folder), FakeApi())
        self.assertEqual(result.status, '无模型')
        self.assertEqual(result.files, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_all_3d_formats_export_without_library_metadata(self):
        exporter = SimpleNamespace(output=SimpleNamespace(raw_wrl='#VRML V2.0 utf8'))
        with self.importer(), patch('backend.Exporter3dModelKicad', return_value=exporter):
            result = download_part('C2040', Options(self.folder, ('STEP', 'WRL', 'OBJ')), FakeApi(obj='v 0 0 0'))
        self.assertEqual(result.status, '成功')
        self.assertEqual({Path(file).suffix for file in result.files}, {'.step', '.wrl', '.obj'})
        metadata = json.loads((Path(result.folder) / 'model-info.json').read_text(encoding='utf-8'))
        self.assertNotIn('altium', metadata)



if __name__ == '__main__':
    unittest.main()
