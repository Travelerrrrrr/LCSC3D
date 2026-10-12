"""Checks for the experimental Type-C placement and self-contained library."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'app')]
from try_typec_embedded_3d import derive_placement, embed, audit_library

BOUNDS = [-4.470030479496271, -3.6650001000000056, -2.580000000000004,
          4.470030479496271, 3.9150000000000063, 1.5810001000000005]


def fixture():
    return json.loads((ROOT / 'app/tests/fixtures/C2765186.json').read_text('utf-8'))


class PlacementTests(unittest.TestCase):
    def test_typec_solder_plane_and_board_insertion_depth_are_preserved(self):
        p = derive_placement(fixture(), BOUNDS)
        self.assertAlmostEqual(p['translation_mm'][0], 0, places=8)
        self.assertAlmostEqual(p['translation_mm'][1], -1.32499755, places=7)
        self.assertAlmostEqual(p['translation_mm'][2], 1.6800018, places=7)
        self.assertAlmostEqual(-1.68 + p['translation_mm'][2], 0, delta=.000005)
        self.assertAlmostEqual(BOUNDS[2] + p['translation_mm'][2], -.9, delta=.000005)

    def test_a_different_model_or_wrong_units_do_not_silently_fit(self):
        scaled = [value * 10 for value in BOUNDS]
        with self.assertRaisesRegex(AssertionError, 'dimensions disagree'):
            derive_placement(fixture(), scaled)

    def test_rotation_is_rejected_until_its_source_semantics_are_verified(self):
        source = fixture()
        document = source['packageDetail']['dataStr']
        if isinstance(document, str):
            document = json.loads(document)
        for i, shape in enumerate(document['shape']):
            if shape.startswith('SVGNODE~'):
                node = json.loads(shape.partition('~')[2])
                node['attrs']['c_rotation'] = '90,0,0'
                document['shape'][i] = 'SVGNODE~' + json.dumps(node)
        source['packageDetail']['dataStr'] = document
        with self.assertRaisesRegex(AssertionError, 'nonzero rotation'):
            derive_placement(source, BOUNDS)


@unittest.skipUnless(sys.platform == 'win32', 'CFB writing requires Windows')
class EmbeddedLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {'LOCALAPPDATA': str(Path(cls.temp.name) / 'LocalAppData')})
        cls.environment.start()
        from app_paths import configure_runtime_paths
        configure_runtime_paths()
        from altium import export_pcblib
        cls.base = export_pcblib(fixture(), 'C2765186')
        cls.step = b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n'
        cls.translation = derive_placement(fixture(), BOUNDS)['translation_mm']

    @classmethod
    def tearDownClass(cls):
        from app_logging import close_logging
        close_logging()
        cls.environment.stop()
        cls.temp.cleanup()

    def test_model_is_embedded_without_altering_pads_and_2d_geometry(self):
        raw = embed(self.base, self.step, self.translation, BOUNDS, 'C2765186.step')
        audit, extracted, _ = audit_library(self.base, raw, self.step)
        self.assertEqual(audit['pads_and_holes'], 18)
        self.assertEqual(audit['unchanged_2d_primitives'], 25)
        self.assertEqual(extracted, self.step)
        self.assertAlmostEqual(audit['translation_mm'][2], 1.6800018, places=7)

    def test_audit_rejects_a_model_id_that_cannot_resolve_its_payload(self):
        import olefile
        from compound_storage import compound_file
        raw = embed(self.base, self.step, self.translation, BOUNDS, 'C2765186.step')
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            streams = {'/'.join(p): ole.openstream(p).read() for p in ole.listdir()}
        metadata = streams['Library/Models/Data']
        start = metadata.index(b'ID={') + len(b'ID={')
        streams['Library/Models/Data'] = metadata[:start] + (b'0' if metadata[start:start+1] != b'0' else b'1') + metadata[start+1:]
        with self.assertRaises(AssertionError):
            audit_library(self.base, compound_file(streams), self.step)

    def test_audit_rejects_changed_step_bytes(self):
        raw = embed(self.base, self.step, self.translation, BOUNDS, 'C2765186.step')
        with self.assertRaisesRegex(AssertionError, 'differs from source'):
            audit_library(self.base, raw, self.step.replace(b'DATA;', b'DATA; '))


if __name__ == '__main__':
    unittest.main()
