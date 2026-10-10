"""Library append destinations and Altium project integration controls."""
from i18n import text as ui_text, message as ui_message
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QWidget)
from localized_widgets import (QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QPushButton)

from altium_project import read_project
from library_merge import read_library
from app_logging import log_event, record_error, traced
from errors import DownloadError
from ui_components import title_block, surface, scroll_page
from shell_ui import choose_path


class ExportTargetsDialog(QDialog):
    def __init__(self, values, parent):
        super().__init__(parent)
        self.setWindowTitle(ui_text('追加 · 已有库与 PCB 工程'))
        self.setWindowModality(Qt.WindowModal)
        self.resize(820, min(740, self.screen().availableGeometry().height() - 60))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(18)
        outer.addWidget(title_block(ui_text('追加 · 已有库与 PCB 工程'), ui_text('选择目标文件，配置库与工程的连接方式'), large=True))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(16)
        outer.addWidget(scroll_page(content), 1)
        hint = QLabel(ui_text('已有库可只选 SchLib 或 PcbLib，只追加对应格式。\n只指定 PCB 工程时，在工程旁按工程名称生成配套库并加入工程；同名库存在时继续追加。\n相同封装自动复用，同名不同封装加序号；可在设置的“备份”页管理自动备份和恢复。'))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        files, files_layout = surface()
        files_layout.addWidget(title_block(ui_text('目标文件')))
        form = QFormLayout()
        form.setSpacing(14)
        files_layout.addLayout(form)
        self.inputs = {}
        for key, title, suffix in (('schlib_target', ui_text('已有符号库'), 'SchLib'),
                                    ('pcblib_target', ui_text('已有封装库'), 'PcbLib'),
                                    ('project_path', ui_text('PCB 工程'), 'PrjPcb')):
            row = QHBoxLayout()
            edit = self.inputs[key] = QLineEdit(values.get(key, ''))
            edit.setAccessibleName(title)
            edit.setPlaceholderText(ui_text('选择 .') + suffix + ui_text(' 文件'))
            row.addWidget(edit)
            browse = QPushButton(ui_text('选择…'))
            browse.clicked.connect(lambda checked=False, e=edit, ext=suffix: self.choose(e, ext))
            row.addWidget(browse)
            clear = QPushButton(ui_text('清除'))
            clear.clicked.connect(edit.clear)
            row.addWidget(clear)
            form.addRow(title, row)
        layout.addWidget(files)
        behavior, behavior_layout = surface()
        behavior_layout.addWidget(title_block(ui_text('导出行为')))
        self.keep_box = QCheckBox(ui_text('独立导出器件'))
        self.keep_box.setChecked(values.get('keep_individual') is True)
        behavior_layout.addWidget(self.keep_box)
        model_hint = QLabel(ui_text('不勾选时，所有 3D 文件集中到 SchLib 旁的“库名_3D”文件夹；仅选 PcbLib 时跟随其名称。\n勾选后，另按器件保存独立库与 3D 文件。'))
        model_hint.setWordWrap(True)
        behavior_layout.addWidget(model_hint)
        self.project_box = QCheckBox(ui_text('将已选已有库导入PCB工程'))
        self.project_box.setChecked(values.get('import_existing_to_project') is True)
        self.project_box.setToolTip(ui_text('同时指定已有库和 PCB 工程后可选；下载完成后将成功追加的库加入工程'))
        behavior_layout.addWidget(self.project_box)
        project_hint = QLabel(ui_text('工程中保存库的文件引用；已打开的 AD 工程需重新加载后查看。\n下载列表为空时，确定后点击主页“导入已有库”即可直接加入工程。'))
        project_hint.setWordWrap(True)
        behavior_layout.addWidget(project_hint)
        layout.addWidget(behavior)
        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton(ui_text('取消'))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton(ui_text('确定'))
        save.setObjectName('primary')
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        outer.addLayout(buttons)
        for edit in self.inputs.values():
            edit.textChanged.connect(self.update_controls)
        self.update_controls()

    def update_controls(self):
        has_library = any(self.inputs[key].text().strip() for key in ('schlib_target', 'pcblib_target'))
        enabled = bool(has_library and self.inputs['project_path'].text().strip())
        self.project_box.setEnabled(enabled)
        if not enabled:
            self.project_box.setChecked(False)

    def choose(self, edit, suffix):
        self.file_page = choose_path(self, ui_text('选择 .') + suffix, edit.text(), edit.setText,
                                     filter=ui_message('AD 文件 (*.{0})', suffix))

    @traced('export.append_settings')
    def save(self):
        self.values = {key: edit.text().strip() for key, edit in self.inputs.items()}
        self.values['keep_individual'] = self.keep_box.isChecked()
        self.values['import_existing_to_project'] = self.project_box.isEnabled() and self.project_box.isChecked()
        try:
            if not any(self.values[key] for key in self.inputs):
                raise DownloadError(ui_text('请至少选择一份已有库或一个 PCB 工程'))
            for key, format in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB')):
                if self.values[key]:
                    read_library(self.values[key], format)
            if self.values['project_path']:
                read_project(self.values['project_path'])
        except Exception as exc:
            record_error(exc, 'export.append_settings_invalid', stage='validate')
            self.status.setText(ui_text('无法使用所选文件：') + str(exc))
            return
        log_event('INFO', 'export.append_settings_saved',
                  existing_formats=[fmt for key, fmt in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB'))
                                    if self.values[key]],
                  project_selected=bool(self.values['project_path']),
                  keep_individual=self.values['keep_individual'],
                  import_project=self.values['import_existing_to_project'])
        self.accept()

    def reject(self):
        log_event('DEBUG', 'export.append_settings_cancelled')
        super().reject()
