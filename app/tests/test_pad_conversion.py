"""Geometry and native-record checks for classes of pads, without a live AD UI."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from altium import export_pcblib
from eda_geometry import pad_outline
from errors import Cancelled, DownloadError

if sys.platform == 'win32':
    from altium_inspect import pcb, region


def rotated(vertices, angle, origin=(0, 0)):
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return [(origin[0]+c*x-s*y, origin[1]-s*x-c*y) for x,y in vertices]


def source_pad(vertices, number='1', layer=1, rotation=0, drill=0, center=(0, 0), paste=0, solder=.2):
    fields = ['PAD', 'POLYGON', str(center[0]), str(center[1]), '4', '2', str(layer), '', number,
              str(drill/2), ' '.join(str(v) for p in vertices for v in p), str(rotation), 'pad', '0', '',
              'Y', '0', str(paste), str(solder), ','.join(map(str, center))]
    return '~'.join(fields)


def data(shapes):
    return {'title': 'Test', 'packageDetail': {'title': 'Test', 'dataStr': {
        'head': {'x': 0, 'y': 0}, 'shape': shapes}}}


class OutlineTests(unittest.TestCase):
    def test_rotated_rectangles_ignore_vendor_type_and_page_origin(self):
        for angle in (0, 37, 90, 180, 270):
            vertices = rotated([(-2,-1),(2,-1),(2,1),(-2,1)], angle, (4000, 3000))
            outline = pad_outline(vertices, angle)
            self.assertEqual(outline.shape, 'RECT')
            self.assertAlmostEqual(outline.size[0], 4)
            self.assertAlmostEqual(outline.size[1], 2)
            self.assertAlmostEqual(outline.center[0], 4000)
            self.assertAlmostEqual(outline.center[1], 3000)

    def test_duplicate_vertices_and_precision_steps_do_not_create_custom_pads(self):
        vertices = [(0,0),(0,0),(2,0),(2,.0006),(4,.0006),(4,2),(2,2),(2,2.0006),(0,2.0006),(0,0)]
        self.assertEqual(pad_outline(vertices).shape, 'RECT')

    def test_outline_orientation_can_override_a_stale_rotation_field(self):
        vertices = rotated([(-2,-1),(2,-1),(2,1),(-2,1)],37)
        outline = pad_outline(vertices,0)
        self.assertEqual(outline.shape,'RECT')
        self.assertAlmostEqual(outline.rotation,37)
        self.assertAlmostEqual(outline.size[0],4)
        self.assertAlmostEqual(outline.size[1],2)

    def test_a_real_notch_or_chamfer_is_not_filled_by_a_bounding_rectangle(self):
        for vertices in [[(0,0),(4,0),(4,2),(2,2),(2,1),(0,1)],
                         [(0,0),(3,0),(4,1),(4,2),(0,2)],
                         [(0,0),(4,0),(4,2),(0,2),(0,.04),(.04,.04),(.04,.02),(0,.02)]]:
            self.assertIsNone(pad_outline(vertices).shape)

    def test_round_and_capsule_contours_use_a_native_round_pad(self):
        for size, angle in [((2,2),0), ((4,2),37), ((2,5),90)]:
            w, h = size
            r, shift = min(w,h)/2, abs(w-h)/2
            points = []
            for side in (0,1):
                for i in range(65):
                    a = -math.pi/2 + side*math.pi + i*math.pi/64
                    u,v = r*math.cos(a)+(shift if side==0 else -shift), r*math.sin(a)
                    points.append((u,v) if w>=h else (-v,u))
            outline = pad_outline(rotated(points, angle), angle)
            self.assertEqual(outline.shape, 'OVAL')
            self.assertAlmostEqual(outline.size[0], w)
            self.assertAlmostEqual(outline.size[1], h)

    def test_ellipse_is_not_changed_into_a_capsule(self):
        vertices = [(2*math.cos(i*math.pi/32), math.sin(i*math.pi/32)) for i in range(64)]
        self.assertIsNone(pad_outline(vertices).shape)

    def test_only_the_native_quarter_width_octagon_is_recognized(self):
        vertices = [(-1.5,-1),(1.5,-1),(2,-.5),(2,.5),(1.5,1),(-1.5,1),(-2,.5),(-2,-.5)]
        self.assertEqual(pad_outline(rotated(vertices, 23),23).shape, 'OCTAGON')
        vertices[0] = (-1.7,-1)
        self.assertIsNone(pad_outline(vertices).shape)

    def test_self_crossing_empty_and_overlarge_contours_fail_explicitly(self):
        for vertices in [[], [(0,0),(1,1)], [(0,0),(2,2),(0,2),(2,0)],
                         [(0,0),(1,0),(2,0)], [(i, i % 2) for i in range(2049)]]:
            with self.assertRaises(DownloadError):
                pad_outline(vertices)

    def test_geometry_checks_honor_cancellation(self):
        def stop(): raise Cancelled()
        with self.assertRaises(Cancelled):
            pad_outline([(0,0),(4,0),(4,2),(0,2)], check_cancelled=stop)


@unittest.skipUnless(sys.platform == 'win32', 'native AD library writer requires Windows')
class NativePadTests(unittest.TestCase):
    def test_shape_recognition_preserves_number_side_rotation_and_manufacturing_layers(self):
        for layer, angle in [(1,37),(2,180)]:
            v = rotated([(-2,-1),(2,-1),(2,1),(-2,1)],angle,(10,20))
            shapes = [source_pad(v,'A4B9',layer,angle,center=(10,20),paste=-393.7,solder=.2008),
                      'SOLIDREGION~5~~M8 19 L12 19 L12 21 L8 21 Z~solid~paste']
            _, pads, others = pcb(export_pcblib(data(shapes),'C1'))
            pad = pads[0]
            self.assertEqual((pad['number'],pad['layer'],pad['shape']),('A4B9',1 if layer==1 else 32,2))
            self.assertEqual((pad['width'],pad['height']),(400000,200000))
            self.assertAlmostEqual(pad['rotation'],angle)
            self.assertEqual((pad['paste'],pad['solder']),(-39370000,20080))
            rows = [region(body) for kind,body in others if kind==11]
            self.assertEqual([layer for layer,_,_ in rows],[35])
            self.assertFalse(any('PADINDEX' in meta for _,meta,_ in rows))

    def test_custom_copper_links_use_one_based_primitive_indices_including_nonpads(self):
        contour = [(0,0),(4,0),(4,2),(2,2),(2,1),(0,1)]
        shapes = ['TRACK~1~3~~-5 -5 5 -5~track', source_pad(contour,'A',center=(3,1)),
                  'SOLIDREGION~5~~M0 0 L1 0 L1 1 L0 1 Z~solid~paste',
                  source_pad([(x+10,y) for x,y in contour],'B',center=(13,1))]
        _, pads, others = pcb(export_pcblib(data(shapes),'C1'))
        linked = [(int(meta['PADINDEX']),v) for kind,body in others if kind==11
                  for layer,meta,v in [region(body)] if 'PADINDEX' in meta]
        self.assertEqual([index for index,_ in linked],[pad['primitive_index']+1 for pad in pads])
        self.assertEqual([index for index,_ in linked],[2,5])
        self.assertEqual([min(p[0] for p in v[0]) for _,v in linked],[0,1000000])

    def test_multilayer_custom_pad_retains_its_drill_and_three_copper_shapes(self):
        contour = [(-4,-3),(4,-3),(4,3),(1,3),(1,1),(-1,1),(-1,3),(-4,3)]
        _, pads, others = pcb(export_pcblib(data([source_pad(contour,'P',11,drill=1,center=(0,0))]),'C1'))
        pad = pads[0]
        self.assertEqual((pad['layer'],pad['drill'],pad['x'],pad['y']),(74,100000,0,0))
        self.assertGreaterEqual(pad['width'],pad['drill'])
        linked = [region(body) for kind,body in others if kind==11]
        self.assertEqual({layer for layer,_,_ in linked},{1,2,32})
        self.assertTrue(all(meta['PADINDEX']=='1' for _,meta,_ in linked))

    def test_recognized_through_hole_preserves_drill_center_and_rotated_copper_offset(self):
        v = rotated([(-2,-1),(2,-1),(2,1),(-2,1)],37,(10,20))
        _, pads, _ = pcb(export_pcblib(data([source_pad(v,'P',11,37,drill=.5,center=(10.1,20.1))]),'C1'))
        pad = pads[0]
        self.assertEqual((pad['shape'],pad['layer'],pad['drill'],pad['x'],pad['y']),(2,74,50000,1010000,-2010000))
        c,s = math.cos(math.radians(37)),math.sin(math.radians(37))
        self.assertAlmostEqual(pad['offset_x'],(-.1*c+.1*s)*100000,delta=1)
        self.assertAlmostEqual(pad['offset_y'],(.1*s+.1*c)*100000,delta=1)
