import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import Cancelled, DownloadError, NetworkApi, Options, atomic_write, download_part, get_component_metadata, parse_part_numbers, safe_filename

STEP = b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'
OBJ = 'newmtl body\nKd 0.25 0.25 0.25\nendmtl\nv 0 0 0\nv 2.54 0 0\nv 0 2.54 0\nusemtl body\nf 1 2 3\n'


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
        return patch('backend.model_reference', return_value=self.model)

    def test_input_dedup_and_invalid_number(self):
        self.assertEqual(parse_part_numbers('c2040，C20197\nC2040; C12bad'), (['C2040', 'C20197'], ['C12bad'], 1))

    def test_component_metadata_reads_title_and_model_without_model_transfers(self):
        api = FakeApi(title=' RP2040 ')
        with self.importer() as importer, patch.object(api, 'get_step_3d_model') as step, \
                patch.object(api, 'get_raw_3d_model_obj') as obj:
            self.assertEqual(get_component_metadata('C2040', api), {'title': 'RP2040', 'model': 'QFN/56'})
        importer.assert_called_once_with({'title': ' RP2040 ', 'lcsc': {'id': 2392}})
        step.assert_not_called()
        obj.assert_not_called()

    def test_component_metadata_keeps_title_without_a_3d_association(self):
        for error in (KeyError('packageDetail'), TypeError('bad shape')):
            with self.subTest(error=error), patch('backend.model_reference', side_effect=error):
                self.assertEqual(get_component_metadata('C2040', FakeApi(title='RP2040')),
                                 {'title': 'RP2040', 'model': ''})
        with patch('backend.model_reference', return_value=None):
            self.assertEqual(get_component_metadata('C2040', FakeApi(title='RP2040')),
                             {'title': 'RP2040', 'model': ''})

    def test_component_metadata_checks_cancellation_before_querying(self):
        api = NetworkApi(threading.Event())
        api.cancelled.set()
        with patch.object(api, 'get_cad_data_of_component') as fetch:
            with self.assertRaises(Cancelled):
                get_component_metadata('C2040', api)
        fetch.assert_not_called()

    def test_step_survives_unavailable_obj(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder, ('STEP', 'OBJ')), FakeApi())
        self.assertEqual(result.status, '部分完成')
        self.assertEqual((self.folder / 'Test component_C2040/C2040_QFN_56.step').read_bytes(), STEP)
        self.assertFalse((self.folder / 'Test component_C2040/C2040_QFN_56.obj').exists())

    def test_rerun_downloads_and_replaces_existing_selected_files(self):
        options = Options(self.folder, ('STEP', 'OBJ'))
        updated_step = STEP.replace(b'HEADER;', b'HEADER; /* refreshed */')
        updated_obj = OBJ.replace('2.54', '5.08')
        with self.importer():
            download_part('C2040', options, FakeApi(obj=OBJ))
            api = FakeApi(step=updated_step, obj=updated_obj)
            with patch.object(api, 'get_raw_3d_model_obj', wraps=api.get_raw_3d_model_obj) as fetch_obj:
                result = download_part('C2040', options, api)
        self.assertEqual(result.status, '成功')
        self.assertEqual(api.step_calls, 1)
        fetch_obj.assert_called_once_with(self.model.uuid)
        self.assertEqual((Path(result.folder) / 'C2040_QFN_56.step').read_bytes(), updated_step)
        self.assertEqual((Path(result.folder) / 'C2040_QFN_56.obj').read_text(), updated_obj)

    def test_failed_refresh_preserves_existing_file(self):
        with self.importer():
            original = download_part('C2040', Options(self.folder), FakeApi())
            result = download_part('C2040', Options(self.folder), FakeApi(step=DownloadError('timeout')))
        self.assertEqual(result.status, '失败')
        self.assertEqual(Path(original.files[0]).read_bytes(), STEP)
        self.assertEqual(set(Path(original.folder).iterdir()), {Path(original.files[0])})

    def test_refresh_does_not_replace_unselected_formats(self):
        with self.importer():
            original = download_part('C2040', Options(self.folder, ('STEP', 'OBJ')), FakeApi(obj=OBJ))
            result = download_part('C2040', Options(self.folder, ('STEP',)), FakeApi(step=STEP + b'\n'))
        self.assertEqual(result.status, '成功')
        self.assertEqual(len(result.files), 1)
        self.assertEqual(Path(result.files[0]).read_bytes(), STEP + b'\n')
        self.assertEqual((Path(original.folder) / 'C2040_QFN_56.obj').read_text(), OBJ)

    def test_failed_file_replacement_keeps_previous_content_and_cleans_up(self):
        path = self.folder / 'library.SchLib'
        path.write_bytes(b'previous library')
        with patch('backend.os.replace', side_effect=PermissionError('file in use')):
            with self.assertRaises(PermissionError):
                atomic_write(path, b'new library')
        self.assertEqual(path.read_bytes(), b'previous library')
        self.assertEqual(list(self.folder.iterdir()), [path])

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
        self.assertEqual(result.title, '温度/传感器:V1')

    def test_missing_title_still_exports_to_number_suffixed_folder(self):
        for data in ({}, {'title': None}, {'title': ''}, {'title': ' \t '}):
            with self.subTest(data=data):
                api = FakeApi()
                with self.importer(), patch.object(api, 'get_cad_data_of_component', return_value=data):
                    result = download_part('C2040', Options(self.folder), api)
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

    def test_ad_filenames_use_only_the_sanitized_component_title(self):
        for title, basename in [(' RP2040 ', 'RP2040'), ('温度/传感器:V1', '温度_传感器_V1'),
                                ('CON', '_CON'), ('x' * 200, 'x' * 115), ('', '未命名器件')]:
            with self.subTest(title=title), patch('backend.model_reference', return_value=None), \
                    patch('backend.export_schlib', return_value=b'schematic'), \
                    patch('backend.export_pcblib', return_value=b'footprint'):
                result = download_part('C2040', Options(self.folder, ('SCHLIB', 'PCBLIB')), FakeApi(title=title))
            self.assertEqual(result.status, '成功', result.message)
            self.assertEqual(Path(result.folder), self.folder / f'{basename}_C2040')
            self.assertEqual({Path(file).name for file in result.files}, {f'{basename}.SchLib', f'{basename}.PcbLib'})
            self.assertEqual({Path(file).read_bytes() for file in result.files}, {b'schematic', b'footprint'})

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

    def test_unsupported_formats_are_rejected_before_network_access(self):
        for formats in (('WRL',), ('SYMBOL',), ('FOOTPRINT',),
                        ('JSON',), ('SVG',), ('STEP', 'SYMBOL'), ()):
            with self.subTest(formats=formats), patch.object(FakeApi, 'get_cad_data_of_component') as fetch:
                with self.assertRaises(DownloadError):
                    download_part('C2040', Options(self.folder, formats), FakeApi())
                fetch.assert_not_called()

    def test_missing_model_does_not_create_output_files(self):
        with patch('backend.model_reference', return_value=None):
            result = download_part('C2040', Options(self.folder), FakeApi())
        self.assertEqual(result.status, '无模型')
        self.assertEqual(result.files, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_official_3d_formats_preserve_payload_without_conversion(self):
        with self.importer():
            result = download_part('C2040', Options(self.folder, ('STEP', 'OBJ')), FakeApi(obj=OBJ))
        self.assertEqual(result.status, '成功')
        self.assertEqual({Path(file).suffix for file in result.files}, {'.step', '.obj'})
        self.assertEqual(set(Path(result.folder).iterdir()), {Path(file) for file in result.files})
        self.assertEqual(next(Path(file) for file in result.files if file.endswith('.obj')).read_text(), OBJ)



if __name__ == '__main__':
    unittest.main()
