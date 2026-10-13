"""Export resource reuse must preserve freshness, cancellation and byte limits."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.core.app_logging import submit_logged
from lcsc3d.models.model_cache import ModelCache, cached_model, reuse_models
from lcsc3d.models.model_export import resolve_models
from lcsc3d.models.model_geometry import obj_origin
from lcsc3d.services.backend import NetworkApi, StepPayload
from lcsc3d.eda.easyeda_import import parse_component
from lcsc3d.eda import altium_models
from lcsc3d.eda.altium import _pcblib_component
from test_model_placement import source, OBJ, FIXTURES


class ModelCacheTests(unittest.TestCase):
    def test_parallel_network_reads_share_one_result_but_next_batch_is_fresh(self):
        api = NetworkApi(use_cache=False)
        first = b'ISO-10303-21; first'
        second = b'ISO-10303-21; second'
        @reuse_models
        def batch():
            with ThreadPoolExecutor(max_workers=3) as pool:
                tasks = [submit_logged(pool, api.get_step_3d_model, 'same-model') for _ in range(3)]
                return [task.result() for task in tasks]
        with patch.object(api, 'fetch', side_effect=[first, second]) as fetch:
            self.assertEqual(batch(), [first] * 3)
            self.assertEqual(batch(), [second] * 3)
            self.assertEqual(fetch.call_count, 2)

    def test_waiters_can_cancel_without_stopping_another_consumer(self):
        cache = ModelCache()
        entered, release, waiting, stop = (threading.Event() for _ in range(4))
        def create():
            entered.set()
            self.assertTrue(release.wait(3))
            return b'complete'
        def check():
            waiting.set()
            if stop.is_set():
                raise Cancelled()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cache.get, 'same', create, len, lambda: None)
            self.assertTrue(entered.wait(2))
            other = pool.submit(cache.get, 'same', lambda: self.fail('Duplicate fetch'), len, check)
            try:
                self.assertTrue(waiting.wait(2))
                stop.set()
                with self.assertRaises(Cancelled):
                    other.result(timeout=1)
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2), b'complete')

    def test_errors_and_missing_resources_can_retry_and_large_entries_are_not_retained(self):
        cache = ModelCache(max_bytes=6, max_entries=2)
        create = Mock(side_effect=[DownloadError('offline'), None, b'1234567', b'ok'])
        with self.assertRaises(DownloadError):
            cache.get('same', create, len, lambda: None)
        self.assertIsNone(cache.get('same', create, len, lambda: None))
        self.assertEqual(cache.get('same', create, len, lambda: None), b'1234567')
        self.assertEqual(cache.size, 0)
        self.assertEqual(cache.get('same', create, len, lambda: None), b'ok')
        self.assertEqual(cache.get('same', create, len, lambda: None), b'ok')
        self.assertEqual(create.call_count, 4)
        for key in ('one', 'two', 'three'):
            cache.get(key, lambda: b'123', len, lambda: None)
        self.assertLessEqual(cache.size, 6)
        self.assertLessEqual(len(cache.items), 2)
        self.assertFalse(cache.pending)

    def test_repeated_asset_keeps_distinct_placements_and_one_embedded_payload(self):
        component = parse_component(source(), 'C20197')
        model = component.models[0]
        position = (model.placement.position[0] + 3, *model.placement.position[1:])
        component = replace(component, models=(model, replace(model,
            source_id='second-position', placement=replace(model.placement, position=position))))
        raw = (FIXTURES / 'model_offset_box.step').read_bytes()
        step = Mock(return_value=StepPayload(raw, identifier=model.identifier))
        origin = Mock(return_value=obj_origin(OBJ))
        component, payloads = resolve_models(component, step, origin_fetch=origin)
        self.assertEqual(step.call_count, 1)
        self.assertEqual(origin.call_count, 1)
        self.assertEqual(component.models[1].translation[0] - component.models[0].translation[0], 3)
        with patch.object(altium_models, 'model_checksum', wraps=altium_models.model_checksum) as checksum, \
                patch.object(altium_models.zlib, 'compress', wraps=altium_models.zlib.compress) as compress:
            _, streams = _pcblib_component(component, component.part, lambda: None, model_assets=payloads)
        self.assertEqual(checksum.call_count, 1)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(struct.unpack('<I', streams['Library/Models/Header']), (1,))
        self.assertEqual(altium_models.zlib.decompress(streams['Library/Models/0']), raw)
