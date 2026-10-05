import sys
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


if __name__ == '__main__':
    unittest.main()
