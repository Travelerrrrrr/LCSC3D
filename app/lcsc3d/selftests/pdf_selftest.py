"""Offline PDF button, missing report, mixed-export and cancellation acceptance."""
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from lcsc3d.services.pdf_download import DatasheetBatch, MISSING_REPORT
from lcsc3d.core.app_logging import get_log_directory


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    pdf = (root / 'fixtures/datasheet.pdf').read_bytes()
    data = {part: json.loads((root/'fixtures'/(part+'.json')).read_text('utf-8')) for part in ('C2040','C20197')}
    state = dict(phase='begin', deadline=time.monotonic()+60, mode='missing', done=False)
    report = dict(version=main.VERSION, frozen=bool(getattr(sys,'frozen',False)))
    output = root/'exports'
    missing = output/'PDF'/MISSING_REPORT
    original_product, original_fetch = DatasheetBatch.product, DatasheetBatch.fetch
    def product(self, part):
        return dict(title='Model-'+part,manufacturer='测试品牌',datasheets=[]
                    if state['mode']=='missing' and part=='C20197' else ['https://example.invalid/'+part+'.pdf'])
    def fetch(self, url, progress):
        if state['mode']=='cancel' and 'C20197' in url:
            self.cancelled.wait(20)
            self.check()
            raise AssertionError('Cancellation not requested')
        payload=pdf+b'\n' if state['mode']=='cancel' else pdf
        progress(len(payload),len(payload))
        return payload
    DatasheetBatch.product, DatasheetBatch.fetch = product, fetch
    class Api(main.NetworkApi):
        def fetch(self,*args,**kwargs):
            raise AssertionError('Unexpected network access')
        def get_cad_data_of_component(self,part):
            if state['phase']=='pdf':
                raise AssertionError('PDF-only queried EDA')
            return data[part]
    main.NetworkApi=Api
    window.request_component_info=lambda:None
    window.show_preview=lambda *a,**k:None
    window.ensure_store(deferred=True)
    timer=QTimer(window)
    timer.setInterval(25)
    def snapshot():
        return {str(p.relative_to(output)):p.read_bytes() for p in output.rglob('*') if p.is_file()}
    def finished():
        return not window.batch_running and not window.worker.isRunning()
    def finish(error=''):
        if state['done']:return
        state['done']=True;timer.stop()
        report.update(success=not error,error=error)
        (root/'pdf-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if window.worker and window.worker.isRunning():window.worker.cancelled.set()
        def close():
            if window.worker and window.worker.isRunning():QTimer.singleShot(50,close)
            else:
                DatasheetBatch.product, DatasheetBatch.fetch = original_product, original_fetch
                window.close();QApplication.instance().exit(1 if error else 0)
        close()
    def tick():
        try:
            if time.monotonic()>state['deadline']:raise AssertionError('PDF acceptance timed out')
            phase=state['phase']
            if phase=='begin':
                assert not window.pdf_box.isChecked()
                window.input.setPlainText('C2040\nC20197');window.load_queue()
                window.path_input.setText(str(output))
                window.step_box.setChecked(False);window.pdf_box.setChecked(True)
                window.bind_3d_box.setChecked(False)
                state['phase']='pdf';window.start_button.click()
                assert window.batch_running and not window.pdf_box.isEnabled()
            elif phase=='pdf' and finished():
                if time.monotonic()-state.setdefault('pdf_settle',time.monotonic())<.4:return
                assert [window.results[p].status for p in window.ids]==['成功','无数据手册']
                assert missing.read_text('utf-8-sig')=='元件型号\t品牌\nModel-C20197\t测试品牌\n'
                assert (output/'PDF/C2040_Model-C2040.pdf').read_bytes()==pdf
                assert any(row['title']=='PDF 数据手册缺失' for row in window.notifications.history.entries)
                assert window.pdf_box.isEnabled()
                report.update(pdf_only=True,missing_report_exact=True,missing_notice=True)
                window.grab().save(str(root/'pdf-missing.png'))
                window.schlib_box.setChecked(True);window.pcblib_box.setChecked(True)
                state['phase']='mixed';window.start_button.click()
            elif phase=='mixed' and finished():
                assert [window.results[p].status for p in window.ids]==['成功','部分完成']
                assert all(any(Path(p).suffix=='.SchLib' for p in row.files) for row in window.results.values())
                report['mixed_exports']=True
                state['before']=snapshot()
                window.schlib_box.setChecked(False);window.pcblib_box.setChecked(False)
                state.update(phase='cancel',mode='cancel');window.start_button.click()
            elif phase=='cancel':
                path=output/'PDF/C2040_Model-C2040.pdf'
                if path.read_bytes()!=pdf+b'\n':return
                window.stop_button.click();state['phase']='cancelled'
            elif phase=='cancelled' and finished():
                if window.progress_bar.value()!=0:return
                assert window.progress_bar.target==0
                assert all(row.status=='已取消' and not row.pdf_status for row in window.results.values())
                assert snapshot()==state['before']
                report['cancel_restores_pdf_and_report']=True
                report['cancel_progress_zero']=True
                state.update(phase='retry',mode='success');window.start_button.click()
            elif phase=='retry' and finished():
                if time.monotonic()-state.setdefault('retry_settle',time.monotonic())<.4:return
                assert all(row.status=='成功' for row in window.results.values())
                assert not missing.exists()
                report['retry_and_stale_report_cleanup']=True
                window.grab().save(str(root/'pdf-success.png'))
                events={json.loads(line)['event'] for line in (get_log_directory()/'LCSC3D.log').read_text('utf-8').splitlines()}
                assert {'download.pdf_saved','download.pdf_missing','download.pdf_report_written','download.rollback_completed'}<=events
                report['diagnostics_verified']=True
                finish()
        except Exception as exc:
            finish(type(exc).__name__+': '+str(exc))
    timer.timeout.connect(tick);timer.start()
