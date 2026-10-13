"""Anonymous frozen-app acceptance against public MCU facets and exact search."""
import json
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from lcsc3d.core.app_logging import record_error
from lcsc3d.ui.favorites import FavoritesDialog


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve(); root.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance(); app.setQuitOnLastWindowClosed(False)
    window.request_component_info = lambda: None
    window.show_preview = lambda *args, **kwargs: None
    dialog = window.bind_store(FavoritesDialog(window, vault=False))
    dialog.request_detail = lambda *_: None
    report = dict(version=main.VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  anonymous=True, saved_session_accessed=False)
    state = dict(phase='category', deadline=time.monotonic() + 90, done=False)
    timer = QTimer(window); timer.setInterval(50)

    def finish(error=''):
        if state['done']:
            return
        state['done'] = True; timer.stop(); dialog.shutdown()
        report.update(success=not error, error=error, stage=state['phase'])
        (root / 'catalog-live-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        def close():
            if dialog.has_jobs():
                QTimer.singleShot(50, close)
            else:
                window.close(); app.exit(1 if error else 0)
        close()

    def tick():
        try:
            if time.monotonic() > state['deadline']:
                raise AssertionError('Anonymous catalog acceptance timed out: ' + state['phase'])
            phase = state['phase']
            if phase == 'category' and not dialog.searching and dialog.search_items:
                dialog.open_filters(); state['phase'] = 'facets'
            elif phase == 'facets' and dialog.filter_cache:
                panel = dialog.filter_panel
                panel.groups.setCurrentRow(next(i for i, group in enumerate(panel.facets) if group['key'] == 'param_31013_n'))
                panel.low.setText('32'); panel.high.setText('64'); panel.unit.setCurrentText('KB'); panel.range_apply.click()
                state.update(phase='range-refresh', ready_after=time.monotonic() + 1)
            elif phase == 'range-refresh' and time.monotonic() >= state['ready_after'] and not dialog.has_jobs() and not dialog.filter_timer.isActive():
                panel = dialog.filter_panel
                assert (panel.low.text(), panel.high.text(), panel.unit.currentText()) == ('32', '64', 'KB')
                visible = [panel.options.item(i).data(Qt.UserRole) for i in range(panel.options.count())
                           if not panel.options.item(i).isHidden()]
                assert '32KB' in visible and '64KB' in visible and '32Byte' not in visible and '128KB' not in visible, visible
                report.update(range_survives_refresh=True, matching_options=visible)
                panel.apply.click(); state['phase'] = 'filtered'
            elif phase == 'filtered' and not dialog.searching and dialog.search_items:
                assert dialog.catalog_filters['parameters'] == {'param_31013_n#range': ['32KB~64KB']}
                report.update(range_applied=True, products=len(dialog.search_items), total=dialog.search_total)
                dialog.search_input.setText('C20197'); dialog.start_search(); state['phase'] = 'exact-search'
            elif phase == 'exact-search' and not dialog.searching and dialog.search_items:
                assert dialog.search_items[0]['part'] == 'C20197'
                assert not dialog.catalog_filters.get('parameters')
                assert dialog.client.account is None and not dialog.vault.exists()
                report['exact_search'] = 'C20197'
                finish()
        except Exception as error:
            record_error(error, 'catalog.acceptance_failed', stage=state['phase'])
            finish(traceback.format_exc())

    window.open_favorites()
    dialog.browse_category({'id': '11329', 'name': '单片机(MCU/MPU/SOC)'})
    timer.timeout.connect(tick); timer.start()
