import copy
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import olefile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_libraries
from backend import Cancelled, Options, download_part

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
STEP = b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'


def sch_records(payload):
    offset = 0
    while offset < len(payload):
        header = struct.unpack_from('<I', payload, offset)[0]
        size, flags = header & 0xFFFFFF, header >> 24
        end = offset + 4 + size
        if end > len(payload):
            raise AssertionError('Truncated SchLib record')
        yield flags, payload[offset + 4:end]
        offset = end


def sch_pins(payload):
    pins = []
    for flags, record in sch_records(payload):
        if flags and struct.unpack_from('<I', record)[0] == 2:
            owner = struct.unpack_from('<h', record, 5)[0]
            cursor = 12 + 1 + record[12] + 13
            length = record[cursor]
            cursor += 1 + length
            length = record[cursor]
            pins.append((record[cursor + 1:cursor + 1 + length].decode('ascii'), owner))
    return pins


def pcb_pads(payload):
    offset = 0

    def block():
        nonlocal offset
        size = struct.unpack_from('<I', payload, offset)[0] & 0xFFFFFF
        start, offset = offset + 4, offset + 4 + size
        if offset > len(payload):
            raise AssertionError('Truncated PcbLib record')
        return payload[start:offset]

    block()  # Component name.
    pads = {}
    while offset < len(payload) and payload[offset] == 2:
        offset += 1
        records = [block() for _ in range(6)]
        name = records[0][1:1 + records[0][0]].decode('ascii')
        x, y, width, height = struct.unpack_from('<iiii', records[4], 13)
        rotation = struct.unpack_from('<d', records[4], 52)[0]
        # One PCB coordinate unit is 1/10000 mil = 0.00000254 mm.
        pads[name] = (x * 0.00000254, y * 0.00000254, width * 0.00000254, height * 0.00000254, rotation)
    return pads


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / 'native/runtime/lcsc-altium.exe').is_file():
            if os.environ.get('LCSC3D_REQUIRE_NATIVE_TESTS') == '1':
                raise RuntimeError('Build the native converter before release tests')
            raise unittest.SkipTest('Native backend is not built')

    def fixture(self, part):
        return json.loads((FIXTURES / f'{part}.json').read_text(encoding='utf-8'))

    def test_real_exports_have_pins_footprint_link_and_embedded_step(self):
        for part, expected_pins in [('C2040', 57), ('C20197', 8)]:
            with self.subTest(part=part):
                exported = export_libraries(self.fixture(part), part, ('SCHLIB', 'PCBLIB'), lambda: None, STEP)
                self.assertFalse(exported.errors)
                self.assertEqual(exported.details['symbol']['pins'], expected_pins)
                self.assertEqual(exported.details['footprint']['pads'], expected_pins)
                with olefile.OleFileIO(io.BytesIO(exported.payloads['SCHLIB'])) as library:
                    self.assertFalse(library.parsing_issues)
                    symbol = next(path for path in library.listdir() if len(path) == 2 and path[-1] == 'Data')
                    payload = library.openstream(symbol).read()
                    pins = sch_pins(payload)
                    self.assertEqual({pin[0] for pin in pins}, {str(i) for i in range(1, expected_pins + 1)})
                    self.assertEqual({pin[1] for pin in pins}, {1})
                    self.assertIn((part + '.PcbLib').encode(), payload)
                    self.assertIn(exported.details['footprint']['name'].encode(), payload)
                with olefile.OleFileIO(io.BytesIO(exported.payloads['PCBLIB'])) as library:
                    self.assertFalse(library.parsing_issues)
                    self.assertEqual(zlib.decompress(library.openstream(['Library', 'Models', '0']).read()), STEP)

    def test_footprint_pads_preserve_geometry_and_convert_canvas_axis(self):
        for part in ('C2040', 'C20197'):
            with self.subTest(part=part):
                data = self.fixture(part)
                source = {fields[8]: tuple(float(fields[i]) for i in (2, 3, 4, 5, 11))
                          for shape in data['packageDetail']['dataStr']['shape'] if shape.startswith('PAD~')
                          for fields in [shape.split('~')]}
                result = export_libraries(data, part, ('PCBLIB',), lambda: None)
                with olefile.OleFileIO(io.BytesIO(result.payloads['PCBLIB'])) as library:
                    footprint = next(path for path in library.listdir() if len(path) == 2 and path[0] != 'Library' and path[-1] == 'Data')
                    pads = pcb_pads(library.openstream(footprint).read())
                self.assertEqual(set(pads), set(source))
                ref = next(iter(source))
                for name, original in source.items():
                    x, y, width, height, rotation = pads[name]
                    self.assertAlmostEqual(x - pads[ref][0], (original[0] - source[ref][0]) * 0.254, delta=0.00001)
                    self.assertAlmostEqual(y - pads[ref][1], -(original[1] - source[ref][1]) * 0.254, delta=0.00001)
                    self.assertAlmostEqual(width, original[2] * 0.254, delta=0.00001)
                    self.assertAlmostEqual(height, original[3] * 0.254, delta=0.00001)
                    self.assertAlmostEqual(rotation, (-original[4]) % 360, delta=0.00001)

    def test_real_library_only_export_without_3d_metadata(self):
        data = copy.deepcopy(self.fixture('C2040'))
        shapes = data['packageDetail']['dataStr']['shape']
        data['packageDetail']['dataStr']['shape'] = [shape for shape in shapes if not shape.startswith('SVGNODE~')]

        class Api:
            check_cancelled = staticmethod(lambda: None)

            def get_cad_data_of_component(self, part):
                return data

            def get_step_3d_model(self, uuid):
                raise AssertionError('No model must mean no STEP download')

        with tempfile.TemporaryDirectory(prefix='中文 AD 导出 ') as name:
            result = download_part('C2040', Options(Path(name), ('SCHLIB', 'PCBLIB')), Api())
            self.assertEqual(result.status, '成功')
            self.assertEqual(len(result.files), 2)
            with olefile.OleFileIO(result.files[1]) as library:
                self.assertFalse(library.exists(['Library', 'Models', '0']))

    def test_bad_symbol_does_not_block_valid_footprint(self):
        data = self.fixture('C2040')
        data.pop('dataStr')
        result = export_libraries(data, 'C2040', ('SCHLIB', 'PCBLIB'), lambda: None)
        self.assertIn('SCHLIB', result.errors)
        self.assertIn('PCBLIB', result.payloads)

    def test_multiple_symbol_units_preserve_pin_ownership(self):
        data = self.fixture('C20197')
        parts = []
        for numbers in ({'1', '2', '7', '8'}, {'3', '4', '5', '6'}):
            block = copy.deepcopy(data['dataStr'])
            block['shape'] = [shape for shape in block['shape']
                              if not shape.startswith('P~') or shape.split('~')[3] in numbers]
            parts.append({'dataStr': block})
        data['subparts'] = parts
        result = export_libraries(data, 'C20197', ('SCHLIB',), lambda: None)
        self.assertEqual(result.details['symbol']['parts'], 2)
        with olefile.OleFileIO(io.BytesIO(result.payloads['SCHLIB'])) as library:
            symbol = next(path for path in library.listdir() if len(path) == 2 and path[-1] == 'Data')
            pins = dict(sch_pins(library.openstream(symbol).read()))
            self.assertEqual(pins, {'1': 1, '2': 1, '7': 1, '8': 1, '3': 2, '4': 2, '5': 2, '6': 2})

    def test_cancel_before_start_does_not_spawn_process(self):
        def cancelled():
            raise Cancelled()

        with patch('altium.subprocess.Popen') as process:
            with self.assertRaises(Cancelled):
                export_libraries({}, 'C2040', ('SCHLIB',), cancelled)
            process.assert_not_called()

    def test_cancel_kills_running_converter(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise Cancelled()

        with patch('altium.subprocess.Popen') as factory:
            process = factory.return_value
            process.poll.return_value = None
            with self.assertRaises(Cancelled):
                export_libraries({}, 'C2040', ('SCHLIB',), cancelled)
            process.kill.assert_called_once()
            process.communicate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
