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

    def test_unexpected_batch_failure_finishes_and_keeps_committed_file_results(self):
        options = backend.Options(self.folder, ('STEP',), bind_3d_models=False)
        path = self.folder / 'C2040.step'; path.write_bytes(STEP)
        saved = backend.Result('C2040', '成功', files=[str(path)], folder=str(self.folder))
        def fail(parts, options, stop, progress, on_result, **kwargs):
            on_result(0, saved)
            raise PermissionError('PRIVATE-stage-path')
        worker = main.BatchWorker(['C2040', 'C20197'], options)
        completed, emitted = [], []
        worker.completed.connect(completed.append)
        worker.result.connect(lambda row, result: emitted.append((row, result)))
        with patch('lcsc3d.ui.main.download_batch', side_effect=fail):
            worker.run()
        self.assertEqual(len(completed), 1)
        first, second = completed[0]
        self.assertEqual((first.status, second.status), ('部分完成', '失败'))
        self.assertEqual(first.files, [str(path)])
        self.assertEqual(path.read_bytes(), STEP)
        self.assertEqual([(row, r.status) for row, r in emitted[-2:]], [(0, '部分完成'), (1, '失败')])
        events = [json.loads(line) for line in (self.folder / 'LCSC3D/logs/LCSC3D.log').read_text('utf-8').splitlines()]
        self.assertTrue(any(row['event'] == 'download.batch_failed' and row['error'] == 'PermissionError' for row in events))
        self.assertNotIn('PRIVATE-stage-path', json.dumps(events))

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

    def test_worker_stop_animates_to_zero_while_waiting_and_ignores_late_updates(self):
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
        release=threading.Event()
        self.addCleanup(release.set)
        class Api(FakeApi):
            def __init__(self,stop,**kwargs):
                super().__init__()
                self.cancelled=stop
            def get_step_3d_model(self,uuid):
                entered.set()
                release.wait(3)
                if self.cancelled.is_set():
                    raise backend.Cancelled()
                return STEP
        with patch('lcsc3d.ui.main.NetworkApi',Api), patch('lcsc3d.services.backend.model_reference',
                return_value=SimpleNamespace(name='model',uuid='test-model')):
            w.start_batch()
            self.assertTrue(entered.wait(2))
            QTest.qWait(400)
            self.assertGreater(w.progress_bar.value(),0)
            self.assertLess(w.progress_bar.value(),10000)
            before = w.progress_bar.value()
            w.stop_button.click()
            self.assertEqual(w.progress_bar.value(), before)
            self.assertEqual(w.progress_bar.target, 0)
            w.update_batch_progress(.99)
            QTest.qWait(90)
            self.assertGreater(w.progress_bar.value(), 0)
            self.assertLess(w.progress_bar.value(), before)
            QTest.qWait(280)
            self.assertTrue(w.batch_running)
            self.assertEqual(w.progress_bar.value(), 0)
            release.set()
            self.assertTrue(w.worker.wait(3000))
            QTest.qWait(50)
            w.update_result(0, backend.Result('C2040', '已取消'))
            w.update_batch_progress(.99)
            self.assertEqual(w.progress_bar.value(), 0)
            self.assertEqual(w.progress_bar.target, 0)
            old_worker = w.worker
            w.start_button.click()
            old_worker.progress_changed.emit(.99)
            old_worker.result.emit(0, backend.Result('C2040', '已取消'))
            self.assertNotEqual(w.progress_bar.target, 9900)
            self.assertNotEqual(getattr(w.results.get('C2040'), 'status', ''), '已取消')
            self.assertTrue(w.worker.wait(3000))
            QTest.qWait(400)
            self.assertEqual(w.results['C2040'].status, '成功')
            self.assertEqual(w.progress_bar.value(), 10000)
        self.assertFalse(w.batch_running)
        self.assertIn('下载已停止',[e['title'] for e in w.notifications.history.entries])
        events=[json.loads(line) for line in (self.folder/'LCSC3D/logs/LCSC3D.log').read_text('utf-8').splitlines()]
        self.assertEqual(sum(row['event']=='download.progress_reset' for row in events),1)

    def test_cancel_reset_reverses_active_animation_without_jumping_or_restarting(self):
        bar=SmoothProgressBar();self.addCleanup(bar.deleteLater)
        bar.setFraction(.6,immediate=True)
        bar.setFraction(.9)
        QTest.qWait(80)
        before=bar.value()
        self.assertTrue(6000<before<9000)
        samples=[];bar.valueChanged.connect(samples.append)
        bar.resetAnimated()
        self.assertEqual(bar.value(),before)
        QTest.qWait(180)
        self.assertTrue(0<bar.value()<before)
        bar.resetAnimated()
        QTest.qWait(180)
        self.assertEqual((bar.value(),bar.target),(0,0))
        self.assertEqual(samples,sorted(samples,reverse=True))
        self.assertGreater(len(set(samples)),3)

    def test_new_run_interrupts_reset_without_old_animation_erasing_new_progress(self):
        bar=SmoothProgressBar();self.addCleanup(bar.deleteLater)
        bar.setFraction(.8,immediate=True);bar.resetAnimated()
        QTest.qWait(60)
        bar.setFraction(0,immediate=True);bar.setFraction(.4)
        QTest.qWait(370)
        self.assertEqual((bar.value(),bar.target),(4000,4000))

    def test_cancelled_completion_including_cleanup_failure_resets_progress(self):
        with patch.object(main.MainWindow,'_setup_preview'),patch.object(main.MainWindow,'request_component_info'),patch.object(main.MainWindow,'show_preview'):
            w=main.MainWindow(settings_enabled=False)
        try:
            w.worker=main.BatchWorker(['C2040'],backend.Options(self.folder),w)
            w.worker.cancelled.set()
            w.progress_bar.setFraction(.7,immediate=True)
            w.complete_batch([backend.Result('C2040','失败',message='清理失败')])
            self.assertEqual(w.progress_bar.target,0)
            QTest.qWait(80)
            self.assertTrue(0<w.progress_bar.value()<7000)
            QTest.qWait(300)
            self.assertEqual(w.progress_bar.value(),0)
        finally:
            w.close();w.deleteLater();self.app.sendPostedEvents(None,QEvent.DeferredDelete)

    def test_editing_cancelled_queue_does_not_count_cancelled_rows_as_progress(self):
        with patch.object(main.MainWindow,'_setup_preview'),patch.object(main.MainWindow,'request_component_info'),patch.object(main.MainWindow,'show_preview'):
            w=main.MainWindow(settings_enabled=False)
            try:
                w.input.setPlainText('C2040\nC20197');w.load_queue()
                w.progress_bar.setFraction(.7,immediate=True)
                w.reset_cancelled_progress()
                for row,part in enumerate(w.ids):
                    w.update_result(row,backend.Result(part,'已取消'))
                w.table.item(1,main.DOWNLOAD_COLUMN).setCheckState(Qt.Unchecked)
                w.remove_checked_downloads()
                self.assertEqual(w.ids,['C20197'])
                self.assertEqual(w.progress_bar.target,0)
                QTest.qWait(360)
                self.assertEqual(w.progress_bar.value(),0)
            finally:
                w.close();w.deleteLater();self.app.sendPostedEvents(None,QEvent.DeferredDelete)
