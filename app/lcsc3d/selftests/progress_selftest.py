"""Offline final-EXE checks for animated progress, notice stacking and records."""
import json
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QTimer, QPoint
from PySide6.QtWidgets import QApplication
from lcsc3d.ui.shell_ui import notify
from lcsc3d.services.notification_history import NotificationHistory
from lcsc3d.services.log_support import package_logs, clear_logs
from lcsc3d.core.app_logging import log_event, get_log_directory


def start(window, folder):
    main=sys.modules[type(window).__module__]
    root=Path(folder).resolve()
    root.mkdir(parents=True,exist_ok=True)
    step=b'ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;'
    state=dict(mode='begin', deadline=time.monotonic()+60, samples=[], done=False)
    release=threading.Event()
    entered=threading.Event()
    report=dict(version=main.VERSION, frozen=bool(getattr(sys,'frozen',False)))
    window.request_component_info=lambda:None
    window.show_preview=lambda *a,**kw:None
    window.ensure_store(deferred=True)
    assert not window.favorites_dialog.ui_ready

    class OfflineApi(main.NetworkApi):
        def fetch(self,*a,**kw):
            raise AssertionError('Unexpected network access')
        def get_cad_data_of_component(self,part):
            return {'title':'Progress example','packageDetail':{'dataStr':{'head':{},'shape':[
                'SVGNODE~'+json.dumps({'attrs':{'uuid':'offline-model','title':'test','c_etype':'outline3D'}})]}}}
        def get_step_3d_model(self,uuid):
            if state.get('cancel_run'):
                return step.replace(b'HEADER;', b'HEADER;\n/* new batch */')
            entered.set()
            while not release.wait(.025):self.check_cancelled()
            self.check_cancelled()
            return step
        def get_raw_3d_model_obj(self,uuid):
            while not self.cancelled.wait(.025):
                self.check_cancelled()
            self.check_cancelled()
    main.NetworkApi=OfflineApi
    timer=QTimer(window)
    timer.setInterval(25)

    def finish(error=''):
        if state['done']:return
        state['done']=True
        timer.stop();release.set()
        report.update(success=not error,error=error)
        (root/'progress-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if window.worker and window.worker.isRunning():window.worker.cancelled.set()
        def close():
            if window.worker and window.worker.isRunning():QTimer.singleShot(50,close)
            else:
                window.close();QApplication.instance().exit(1 if error else 0)
        close()

    def tick():
        try:
            if time.monotonic()>state['deadline']:raise AssertionError('Progress verification timeout')
            mode=state['mode']
            if mode=='begin':
                window.input.setPlainText('C2040');window.load_queue()
                window.path_input.setText(str(root/'export'))
                window.step_box.setChecked(True);window.obj_box.setChecked(False)
                window.schlib_box.setChecked(False);window.pcblib_box.setChecked(False)
                window.start_button.click()
                state.update(mode='blocked',started=time.monotonic())
            elif mode=='blocked':
                state['samples'].append(window.progress_bar.value())
                if entered.is_set() and time.monotonic()-state['started']>.65:
                    assert 0<window.progress_bar.value()<10000,state['samples']
                    assert len(set(state['samples']))>3,state['samples']
                    window.grab().save(str(root/'progress-during-download.png'))
                    report['download_intermediate_values']=sorted(set(state['samples']))
                    release.set();state.update(mode='completed',started=time.monotonic())
            elif mode=='completed' and not window.batch_running and not window.worker.isRunning():
                if window.progress_bar.value()!=10000:return
                assert window.results['C2040'].status=='成功'
                assert any(Path(p).read_bytes()==step for p in window.results['C2040'].files)
                report['full_after_commit']=True
                report['anonymous_deferred_store_download']=not window.favorites_dialog.ui_ready
                state['before']={str(p):p.read_bytes() for p in (root/'export').rglob('*') if p.is_file()}
                state['cancel_run']=True
                window.input.setPlainText('C2040\nC20197');window.load_queue()
                window.obj_box.setChecked(True)
                window.start_button.click()
                state['mode']='cancel-written'
            elif mode=='cancel-written':
                files=list((root/'export').rglob('*.step'))
                if len(files)<2 or not all(b'new batch' in p.read_bytes() for p in files):return
                window.ensure_store()
                assert not window.favorites_dialog.cart_panel.import_enabled
                assert not window.favorites_dialog.orders_panel.detail.import_enabled
                window.stop_button.click()
                state['mode']='cancelled'
            elif mode=='cancelled' and not window.batch_running and not window.worker.isRunning():
                assert all(r.status=='已取消' and not r.files for r in window.results.values())
                assert {str(p):p.read_bytes() for p in (root/'export').rglob('*') if p.is_file()}==state['before']
                assert not list((root/'export').glob('*_C20197'))
                assert window.start_button.isEnabled()
                assert window.favorites_dialog.cart_panel.import_enabled
                report['cancel_removes_new_and_restores_previous']=True
                report['store_open_during_download']=True
                state['cancel_run']=False
                window.obj_box.setChecked(False)
                window.start_button.click()
                state['mode']='retried'
            elif mode=='retried' and not window.batch_running and not window.worker.isRunning():
                assert all(r.status=='成功' for r in window.results.values())
                report['retry_after_cancel']=True
                for toast in list(window.notifications.toasts):toast.close()
                state['older']=notify(window,'Earlier notice','Earlier detail',severity='info',persistent=True)
                state.update(mode='stack-second',started=time.monotonic())
            elif mode=='stack-second' and time.monotonic()-state['started']>.28:
                state['older_y']=state['older'].y()
                state['newer']=notify(window,'Latest notice','Latest detail',severity='warning',persistent=True)
                state.update(mode='stack-check',started=time.monotonic())
            elif mode=='stack-check' and time.monotonic()-state['started']>.30:
                old,new=state['older'],state['newer']
                assert old.y()>state['older_y'] and new.y()<old.y()
                assert all(t.geometry().right()==t.parentWidget().width()-17 for t in (old,new))
                assert new.y()>window.title_bar.mapTo(new.parentWidget(),QPoint(0,window.title_bar.height())).y()
                window.grab().save(str(root/'notification-stack.png'))
                report['newest_top_right']=report['older_moves_down']=True
                window.messages_button.click()
                page=window.messages_page
                assert page.table.rowCount()==5
                assert page.rows[0]['title']=='Latest notice'
                page.copy_button.click()
                assert 'Latest detail' in QApplication.clipboard().text()
                history=NotificationHistory().entries
                assert len(history)==5
                assert sum(row['title']=='下载已停止' for row in history)==1
                window.grab().save(str(root/'messages.png'))
                report['history_saved_reloaded']=report['messages_page_and_copy']=True
                logs=Path(get_log_directory())/'LCSC3D.log'
                events=[json.loads(line) for line in logs.read_text('utf-8').splitlines()]
                assert all(event in {r['event'] for r in events} for event in
                    ('download.progress','download.batch_result','download.rollback_completed',
                     'notification.created','notification.history_opened'))
                report['progress_and_notification_logs']=True
                bundle=package_logs()
                assert bundle['path'].is_file()
                before=window.notifications.history.path.read_bytes()
                cleared=clear_logs();assert not cleared['failures'],cleared
                assert before==window.notifications.history.path.read_bytes()
                assert log_event('INFO','verification.after_log_clear')
                assert 'verification.after_log_clear' in logs.read_text('utf-8')
                report['logs_packaged_and_writable_after_clear']=True
                report['message_history_survives_log_clear']=True
                report['single_native_window']=[w for w in QApplication.topLevelWidgets() if w.isVisible()]==[window]
                assert report['single_native_window']
                finish()
        except Exception as exc:finish(type(exc).__name__+': '+str(exc))
    timer.timeout.connect(tick)
    timer.start()
