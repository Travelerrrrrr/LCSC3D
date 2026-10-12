"""Check observable progress before completion, smooth motion and cancellation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from lcsc3d.services import backend
from lcsc3d.ui import main
from lcsc3d.core.app_logging import close_logging
from lcsc3d.services.download_progress import BatchProgress
from lcsc3d.ui.progress_ui import SmoothProgressBar
from test_backend import FakeApi, STEP


class ProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        env = patch.dict(os.environ, LOCALAPPDATA=folder.name)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(close_logging)

    def test_single_download_reports_stages_and_reaches_full_only_after_file_commit(self):
        samples = []
        def update(value):
            samples.append((value, bool(list(self.folder.rglob('*.step')))))
        with patch('lcsc3d.services.backend.model_reference', return_value=SimpleNamespace(name='model', uuid='test-model')):
            rows = backend.download_batch(['C2040'],backend.Options(self.folder,('STEP',),bind_3d_models=False),
                api=FakeApi(step=STEP),on_progress=update)
        self.assertEqual(rows[0].status,'成功')
        self.assertTrue(any(0 < p < .5 and not exists for p,exists in samples))
        self.assertEqual(samples[-1],(1.,True))
        self.assertEqual([v for v,_ in samples],sorted(v for v,_ in samples))

    def test_ad_merge_and_kicad_publish_have_intermediate_progress_and_keep_full_for_committed_files(self):
        source=json.loads((Path(__file__).parent/'fixtures/C20197.json').read_text('utf-8'))
        for fmt in ('PCBLIB','KICAD_FOOTPRINT'):
            with self.subTest(fmt=fmt):
                api=backend.NetworkApi(use_cache=False)
                api.get_cad_data_of_component=lambda part:source
                output=self.folder/fmt
                samples=[]
                rows=backend.download_batch(['C20197'],backend.Options(output,(fmt,),merge_pcblib=fmt=='PCBLIB',
                    bind_3d_models=False),api=api,on_progress=lambda p:samples.append((p,len(list(output.rglob('*'))))))
                self.assertEqual(rows[0].status,'成功',rows[0].message)
                self.assertIn(.8,[p for p,_ in samples])
                self.assertTrue(any(.8 < p < 1 for p,_ in samples))
                self.assertEqual(samples[-1][0],1)
                self.assertTrue(all(Path(p).is_file() for p in rows[0].files))

    def test_cancelled_batch_does_not_report_full_or_advance_unstarted_parts(self):
        stop=threading.Event()
        class Api(FakeApi):
            cancelled=stop
            def get_cad_data_of_component(self,part):
                stop.set()
                raise backend.Cancelled()
        samples=[]
        rows=backend.download_batch(['C2040','C20197'],backend.Options(self.folder,('STEP',),bind_3d_models=False),
            cancelled=stop,api=Api(),workers=1,on_progress=samples.append)
        self.assertTrue(all(r.status=='已取消' for r in rows))
        self.assertEqual(samples,[.01])

    def test_concurrent_parts_produce_monotonic_aggregate_progress(self):
        samples=[]
        tracker=BatchProgress(['C1','C2'],samples.append)
        barrier=threading.Barrier(2)
        def run(part):
            barrier.wait(2)
            for value in (.2,.4,.1,.8):tracker.advance(part,value)
        threads=[threading.Thread(target=run,args=(p,)) for p in ('C1','C2')]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertFalse(any(t.is_alive() for t in threads))
        tracker.finish()
        self.assertEqual(samples,sorted(set(samples)))
        self.assertEqual(samples[-1],1)

    def test_animation_has_intermediate_values_settles_and_resets_without_old_motion(self):
        bar=SmoothProgressBar()
        self.addCleanup(bar.deleteLater)
        bar.setFraction(.1)
        self.assertEqual(bar.value(),0)
        QTest.qWait(90)
        self.assertGreater(bar.value(),0)
        self.assertLess(bar.value(),1000)
        bar.setFraction(.25)
        QTest.qWait(360)
        self.assertEqual(bar.value(),2500)
        bar.setFraction(.1)
        self.assertEqual(bar.target,2500)
        bar.setFraction(0,immediate=True)
        QTest.qWait(100)
        self.assertEqual(bar.value(),0)

    def test_worker_shows_progress_during_blocked_download_and_stop_preserves_it(self):
        with patch.object(main.MainWindow,'_setup_preview'), patch.object(main.MainWindow,'request_component_info'), \
                patch.object(main.MainWindow,'show_preview'):
            w=main.MainWindow(settings_enabled=False)
        w.request_component_info=lambda:None
        w.show_preview=lambda *args,**kwargs:None
        def close():
            w.close();w.deleteLater();self.app.sendPostedEvents(None,QEvent.DeferredDelete)
        self.addCleanup(close)
        w.input.setPlainText('C2040')
        with patch.object(w,'request_component_info'),patch.object(w,'show_preview'):w.load_queue()
        w.path_input.setText(str(self.folder/'exports'))
        entered=threading.Event()
        class Api(FakeApi):
            def __init__(self,stop,**kwargs):
                super().__init__()
                self.cancelled=stop
            def get_step_3d_model(self,uuid):
                entered.set()
                self.cancelled.wait(3)
                raise backend.Cancelled()
        with patch('lcsc3d.ui.main.NetworkApi',Api), patch('lcsc3d.services.backend.model_reference',
                return_value=SimpleNamespace(name='model',uuid='test-model')):
            w.start_batch()
            self.assertTrue(entered.wait(2))
            QTest.qWait(400)
            self.assertGreater(w.progress_bar.value(),0)
            self.assertLess(w.progress_bar.value(),10000)
            w.stop_batch()
            self.assertTrue(w.worker.wait(3000))
            QTest.qWait(450)
        self.assertFalse(w.batch_running)
        self.assertLess(w.progress_bar.target,10000)
        self.assertIn('下载已停止',[e['title'] for e in w.notifications.history.entries])
