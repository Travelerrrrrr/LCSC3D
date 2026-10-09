"""Library append destinations and Altium project integration controls."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout)

from altium_project import read_project
from library_merge import read_library
from app_logging import log_event, record_error, traced
from errors import DownloadError


class ExportTargetsDialog(QDialog):
    def __init__(self, values, parent):
        super().__init__(parent)
        self.setWindowTitle('追加 · 已有库与 PCB 工程')
        self.setWindowModality(Qt.WindowModal)
        self.resize(660, 350)
        layout = QVBoxLayout(self)
        hint = QLabel('已有库可只选 SchLib 或 PcbLib，只追加对应格式。\n只指定 PCB 工程时，在工程旁按工程名称生成配套库并加入工程；同名库存在时继续追加。\n相同封装自动复用，同名不同封装加序号；原有内容保留，修改前自动备份到应用数据目录。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.inputs = {}
        for key, title, suffix in (('schlib_target', '已有符号库', 'SchLib'),
                                    ('pcblib_target', '已有封装库', 'PcbLib'),
                                    ('project_path', 'PCB 工程', 'PrjPcb')):
            row = QHBoxLayout()
            edit = self.inputs[key] = QLineEdit(values.get(key, ''))
            edit.setAccessibleName(title)
            edit.setPlaceholderText('选择 .' + suffix + ' 文件')
            row.addWidget(edit)
            browse = QPushButton('选择…')
            browse.clicked.connect(lambda checked=False, e=edit, ext=suffix: self.choose(e, ext))
            row.addWidget(browse)
            clear = QPushButton('清除')
            clear.clicked.connect(edit.clear)
            row.addWidget(clear)
            form.addRow(title, row)
        layout.addLayout(form)
        self.keep_box = QCheckBox('独立导出器件')
        self.keep_box.setChecked(values.get('keep_individual') is True)
        layout.addWidget(self.keep_box)
        model_hint = QLabel('不勾选时，所有 3D 文件集中到 SchLib 旁的“库名_3D”文件夹；仅选 PcbLib 时跟随其名称。\n勾选后，另按器件保存独立库与 3D 文件。')
        model_hint.setWordWrap(True)
        layout.addWidget(model_hint)
        self.project_box = QCheckBox('将已选已有库导入PCB工程')
        self.project_box.setChecked(values.get('import_existing_to_project') is True)
        self.project_box.setToolTip('同时指定已有库和 PCB 工程后可选；下载完成后将成功追加的库加入工程')
        layout.addWidget(self.project_box)
        project_hint = QLabel('工程中保存库的文件引用；已打开的 AD 工程需重新加载后查看。\n下载列表为空时，确定后点击主页“导入已有库”即可直接加入工程。')
        project_hint.setWordWrap(True)
        layout.addWidget(project_hint)
        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton('确定')
        save.setObjectName('primary')
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        layout.addLayout(buttons)
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
        path, _ = QFileDialog.getOpenFileName(self, '选择 .' + suffix, edit.text(), f'AD 文件 (*.{suffix})')
        if path:
            edit.setText(path)

    @traced('export.append_settings')
    def save(self):
        self.values = {key: edit.text().strip() for key, edit in self.inputs.items()}
        self.values['keep_individual'] = self.keep_box.isChecked()
        self.values['import_existing_to_project'] = self.project_box.isEnabled() and self.project_box.isChecked()
        try:
            if not any(self.values[key] for key in self.inputs):
                raise DownloadError('请至少选择一份已有库或一个 PCB 工程')
            for key, format in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB')):
                if self.values[key]:
                    read_library(self.values[key], format)
            if self.values['project_path']:
                read_project(self.values['project_path'])
        except Exception as exc:
            record_error(exc, 'export.append_settings_invalid', stage='validate')
            self.status.setText('无法使用所选文件：' + str(exc))
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
