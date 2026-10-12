"""Bounds reuse, bounded metadata storage and cancellable single-worker scheduling."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_paths import temporary_directory
from errors import Cancelled, DownloadError
import model_geometry as geometry

STEP = b'ISO-10303-21; controlled cache input'
BOUNDS = (-1., -2., -.5, 1., 2., 2.5)


class StepBoundsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=temporary_directory())
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, LOCALAPPDATA=str(self.root/'profile'))
        environment.start()
        self.addCleanup(environment.stop)
        geometry._CACHE.clear()

    def test_cache_survives_process_cache_reset_and_contains_no_model_payload(self):
        with patch.object(geometry, '_run', return_value=BOUNDS) as run:
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)
            geometry._CACHE.clear()
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)
            run.assert_called_once()
        files = list((self.root/'profile/LCSC3D/cache').rglob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertLess(files[0].stat().st_size, 512)
        self.assertEqual(set(json.loads(files[0].read_text('utf-8'))), {'sha256', 'bounds'})

    def test_content_change_causes_new_read_and_corrupt_cache_is_replaced(self):
        with patch.object(geometry, '_run', return_value=BOUNDS) as run:
            geometry.step_bounds(STEP)
            geometry.step_bounds(STEP+b'changed')
            self.assertEqual(run.call_count, 2)
            for path in (self.root/'profile/LCSC3D/cache').rglob('*.json'):
                path.write_text('{bad cache', encoding='utf-8')
            geometry._CACHE.clear()
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)
            self.assertEqual(run.call_count, 3)

    def test_cache_metadata_is_bounded_on_disk_and_in_memory(self):
        with patch.object(geometry, '_CACHE_LIMIT', 2), patch.object(geometry, '_DISK_CACHE_LIMIT', 3), \
                patch.object(geometry, '_run', return_value=BOUNDS):
            for i in range(6):
                geometry.step_bounds(STEP+str(i).encode())
        self.assertEqual(len(geometry._CACHE), 2)
        self.assertEqual(len(list((self.root/'profile/LCSC3D/cache').rglob('*.json'))), 3)
        self.assertTrue(all(value == BOUNDS for value in geometry._CACHE.values()))

    def test_unwritable_optional_cache_does_not_break_export(self):
        with patch.object(geometry, '_run', return_value=BOUNDS), \
                patch.object(Path, 'mkdir', side_effect=PermissionError):
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)

    def test_concurrent_same_content_launches_only_one_worker(self):
        entered, release, second_checked = threading.Event(), threading.Event(), threading.Event()
        def run(*_):
            entered.set()
            self.assertTrue(release.wait(5))
            return BOUNDS
        with patch.object(geometry, '_run', side_effect=run) as worker, ThreadPoolExecutor(2) as pool:
            first = pool.submit(geometry.step_bounds, STEP)
            self.assertTrue(entered.wait(5))
            second = pool.submit(geometry.step_bounds, STEP, second_checked.set)
            try:
                self.assertTrue(second_checked.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(5), BOUNDS)
            self.assertEqual(second.result(5), BOUNDS)
            worker.assert_called_once()

    def test_cancelled_waiter_does_not_start_worker_or_poison_queue(self):
        count = 0
        def check():
            nonlocal count
            count += 1
            if count > 1:
                raise Cancelled()
        geometry._WORKERS.acquire()
        try:
            with patch.object(geometry, '_run') as worker:
                with self.assertRaises(Cancelled):
                    geometry.step_bounds(STEP, check)
                worker.assert_not_called()
        finally:
            geometry._WORKERS.release()
        with patch.object(geometry, '_run', return_value=BOUNDS):
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)

    def test_failure_is_not_cached_and_retry_can_succeed(self):
        with patch.object(geometry, '_run', side_effect=[DownloadError('broken'), BOUNDS]) as run:
            with self.assertRaises(DownloadError):
                geometry.step_bounds(STEP)
            self.assertEqual(geometry.step_bounds(STEP), BOUNDS)
            self.assertEqual(run.call_count, 2)

    def test_cancelling_active_process_terminates_it_and_removes_temporary_files(self):
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [None]
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise Cancelled()
        with patch.object(geometry.subprocess, 'Popen', return_value=process):
            with self.assertRaises(Cancelled):
                geometry._run(STEP, check)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertFalse(list(temporary_directory().glob('model-geometry-*')))


if __name__ == '__main__':
    unittest.main()
