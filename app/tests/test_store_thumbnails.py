"""Thumbnail traffic stays bounded and cancellation stops queued photo requests."""
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core.errors import Cancelled
from lcsc3d.selftests.favorites_selftest import fixture_png
from lcsc3d.ui.store_images import load_search_thumbnails, PhotoCache
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


class ThumbnailLoadingTests(unittest.TestCase):
    def test_decoded_photo_cache_is_bounded_and_evicts_least_recently_used_pixels(self):
        pixmap = QPixmap(128, 128)
        pixmap.fill(Qt.red)
        cache = PhotoCache(max_bytes=2 * PhotoCache.cost(pixmap))
        cache[0] = pixmap
        cache[1] = pixmap
        self.assertEqual(cache[0].toImage().pixelColor(0, 0).name(), '#ff0000')
        cache[2] = pixmap
        self.assertNotIn(1, cache)
        large = QPixmap(512, 512)
        cache[3] = large
        self.assertNotIn(3, cache)
        self.assertLessEqual(cache.size, cache.max_bytes)
        cache.clear()
        self.assertEqual(cache.size, 0)

    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def test_loads_photos_with_at_most_three_concurrent_requests(self):
        lock = threading.Lock()
        active = peak = 0
        loaded = []
        def image(url, stop):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return fixture_png()
        load_search_thumbnails({str(n): [n] for n in range(20)}, lambda: SimpleNamespace(image=image),
                               threading.Event(), lambda row, value: loaded.append((row, value)))
        self.assertEqual(peak, 3)
        self.assertEqual(sorted(row for row, _ in loaded), list(range(20)))
        self.assertTrue(all(not value[1].isNull() for _, value in loaded))

    def test_cancel_stops_pending_photos_and_no_more_results_are_reported(self):
        stop = threading.Event()
        requested, loaded = [], []
        def image(url, cancelled):
            requested.append(url)
            time.sleep(0.03)
            return fixture_png()
        def progress(row, result):
            loaded.append(row)
            stop.set()
        with self.assertRaises(Cancelled):
            load_search_thumbnails({str(n): [n] for n in range(20)}, lambda: SimpleNamespace(image=image), stop, progress)
        self.assertEqual(len(loaded), 1)
        self.assertLess(len(requested), 20)


if __name__ == '__main__':
    unittest.main()
