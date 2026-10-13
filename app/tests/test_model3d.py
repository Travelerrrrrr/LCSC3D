"""Validate official model association and geometry for local preview."""
import json
import base64
import struct
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.models.model3d import ModelReference, model_reference, read_obj
from test_backend import OBJ


class ModelTests(unittest.TestCase):
    def test_real_footprints_choose_explicit_model_uuid(self):
        fixtures = Path(__file__).parent / 'fixtures'
        for part, uuid, name in (
            ('C2040', '76b360a9d4c54384a4e47d7e5af156df', 'LQFN-56_L7.0-W7.0-P0.4-EP'),
            ('C20197', '551b3dd0a237409fa823f51b33d3f6d1', 'R0603-8P_L3.2-W1.6-H0.6')):
            data = json.loads((fixtures / f'{part}.json').read_text(encoding='utf-8'))
            self.assertEqual(model_reference(data), ModelReference(name, uuid))
            data['packageDetail']['dataStr'] = json.dumps(data['packageDetail']['dataStr'])
            self.assertEqual(model_reference(data).uuid, uuid)

    def test_missing_and_legacy_model_associations(self):
        self.assertIsNone(model_reference({'title': 'symbol only'}))
        data = {'packageDetail': {'title': 'QFN', 'dataStr': {'head': {'uuid_3d': 'legacy-id'}}}}
        self.assertEqual(model_reference(data), ModelReference('QFN', 'legacy-id'))
        data['packageDetail']['dataStr']['head']['uuid_3d'] = '../../model'
        with self.assertRaises(DownloadError):
            model_reference(data)

    def test_preview_preserves_mm_dimensions_material_and_face_indices(self):
        mesh = read_obj(OBJ)
        self.assertEqual(mesh.vertices, [(0, 0, 0), (2.54, 0, 0), (0, 2.54, 0)])
        self.assertEqual(mesh.faces, [('body', (0, 1, 2))])
        self.assertEqual(mesh.materials['body'].diffuse, (0.25, 0.25, 0.25))
        self.assertEqual(mesh.preview_payload()['faces'][0][1:], [0, 1, 2])
        self.assertEqual(mesh.preview_payload()['vertices'], mesh.vertices)

    def test_relative_indices_slashes_polygons_and_tabs(self):
        raw = OBJ.replace('f 1 2 3', 'v 2.54 2.54 0\nf\t-4/1/2 -3//2 -1/4/2 -2')
        self.assertEqual(read_obj(raw).faces[0][1], (0, 1, 3, 2))

    def test_multiple_materials_keep_face_assignments(self):
        raw = OBJ.replace('endmtl', 'd 0.0\nendmtl') + 'usemtl pin\nf 1 3 2\n'
        mesh = read_obj(raw)
        compact = read_obj(raw, packed=True)
        self.assertEqual(compact.preview_payload(), mesh.preview_payload())
        self.assertEqual(compact.preview_transfer(), mesh.preview_transfer())
        self.assertEqual(len(mesh.vertices), 3)
        self.assertEqual([name for name, _ in mesh.faces], ['body', 'pin'])

    def test_bad_models_do_not_produce_placeholder_geometry(self):
        for raw in ('<html>blocked</html>', 'v 0 0 0', OBJ.replace('2.54', 'nan'),
                    OBJ.replace('f 1 2 3', 'f 0 2 3'), OBJ.replace('f 1 2 3', 'f 1 2 4'),
                    OBJ.replace('f 1 2 3', 'f -4 -2 -1')):
            with self.subTest(raw=raw), self.assertRaises(DownloadError):
                read_obj(raw)

    def test_reader_obeys_cancellation(self):
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            read_obj(OBJ, cancel)

    def test_binary_preview_preserves_full_precision_materials_and_polygons(self):
        raw = OBJ.replace('2.54', '2.540000000000001') + 'usemtl second\nf -3 -2 -1\n'
        mesh = read_obj(raw)
        payload = json.loads(mesh.preview_transfer())
        vertices = base64.b64decode(payload['vertices'])
        self.assertEqual(struct.unpack('<' + 'd' * (len(vertices) // 8), vertices),
                         tuple(v for vertex in mesh.vertices for v in vertex))
        faces = base64.b64decode(payload['faces'])
        decoded = struct.unpack('<' + 'I' * (len(faces) // 4), faces)
        self.assertEqual(decoded, (1, 3, 0, 1, 2, 2, 3, 0, 1, 2))
        self.assertEqual(payload['materials'], mesh.preview_payload()['materials'])
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            mesh.preview_transfer(cancel)

    def test_compact_reader_preserves_line_numbers_and_all_splitlines_separators(self):
        for separator in ('\n', '\r\n', '\r', '\v', '\f', '\x85', '\u2028', '\u2029'):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(read_obj(separator.join(OBJ.splitlines()), packed=True).preview_payload(), read_obj(OBJ).preview_payload())
                with self.assertRaisesRegex(DownloadError, '第 3 行'):
                    read_obj(separator.join(('', '# comment', 'v nan 0 0')), packed=True)


if __name__ == '__main__':
    unittest.main()
