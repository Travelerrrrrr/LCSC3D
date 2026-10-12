"""Library append destinations and Altium project integration controls."""
from lcsc3d.core.i18n import text as ui_text, message as ui_message
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QWidget)
from lcsc3d.ui.localized_widgets import (QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QPushButton)

from lcsc3d.eda.altium_project import read_project
from lcsc3d.services.backend import library_filename
from lcsc3d.eda.library_merge import read_library
from lcsc3d.core.app_logging import log_event, record_error, traced
from lcsc3d.core.errors import DownloadError
from lcsc3d.ui.ui_components import title_block, surface, scroll_page
from lcsc3d.ui.shell_ui import choose_path


TARGET_PATH_KEYS = ('schlib_target', 'pcblib_target', 'project_path')
PROJECT_OUTPUT_KEYS = ('project_model_directory', 'project_schlib_directory', 'project_schlib_name',
                       'project_pcblib_directory', 'project_pcblib_name')
APPEND_TEXT_KEYS = TARGET_PATH_KEYS + PROJECT_OUTPUT_KEYS + ('individual_directory',)


class ExportTargetsDialog(QDialog):
    import_requested = Signal(object)

    def __init__(self, values, parent):
        super().__init__(parent)
        self.import_running = False
        self.setWindowTitle(ui_text('追加 · 已有库与 PCB 工程'))
        self.setWindowModality(Qt.WindowModal)
        self.resize(820, min(740, self.screen().availableGeometry().height() - 60))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(18)
        outer.addWidget(title_block(ui_text('追加 · 已有库与 PCB 工程'), ui_text('选择目标文件，配置库与工程的连接方式'), large=True))
        content = self.content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(16)
        self.content_scroll = scroll_page(content)
        outer.addWidget(self.content_scroll, 1)
        hint = QLabel(ui_text('已有库可只选 SchLib 或 PcbLib，只追加对应格式。\n只指定 PCB 工程时，可分别设置 3D、SchLib 和 PcbLib 的导出目录及库名；同名库存在时继续追加。\n相同封装自动复用，同名不同封装加序号；可在设置的“备份”页管理自动备份和恢复。'))
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
        self.import_button = QPushButton(ui_text('将指定库导入到PCB工程'))
        self.import_button.setToolTip(ui_text('将上方指定lib导入到指定的PCB工程中。'))
        self.import_button.clicked.connect(self.request_import)
        files_layout.addWidget(self.import_button, 0, Qt.AlignLeft)
        project_hint = QLabel(ui_text('将上方指定lib导入到指定的PCB工程中。'))
        project_hint.setWordWrap(True)
        files_layout.addWidget(project_hint)
        layout.addWidget(files)

        self.project_outputs, project_layout = surface()
        project_layout.addWidget(title_block(ui_text('工程配套库与 3D'),
                                            ui_text('仅指定 PCB 工程时生效；目录和库名留空时使用默认值。')))
        project_form = QFormLayout()
        project_form.setSpacing(14)
        project_layout.addLayout(project_form)
        self.add_directory(project_form, 'project_model_directory', ui_text('3D 导出目录'), values)
        for kind, label in (('schlib', 'SchLib'), ('pcblib', 'PcbLib')):
            self.add_directory(project_form, f'project_{kind}_directory', ui_message('{0} 导出目录', label), values)
            edit = self.inputs[f'project_{kind}_name'] = QLineEdit(values.get(f'project_{kind}_name', ''))
            edit.setAccessibleName(ui_message('{0} 库名', label))
            project_form.addRow(ui_message('{0} 库名', label), edit)
        project_model_hint = QLabel(ui_text('库默认保存在 PCB 工程目录，名称默认与工程同名。3D 默认集中到“库名_3D”目录；独立导出时按器件分目录，若指定 3D 目录则在该目录下保存。'))
        project_model_hint.setWordWrap(True)
        project_layout.addWidget(project_model_hint)
        layout.addWidget(self.project_outputs)

        behavior, behavior_layout = surface()
        behavior_layout.addWidget(title_block(ui_text('导出行为')))
        self.keep_box = QCheckBox(ui_text('独立导出器件'))
        self.keep_box.setChecked(values.get('keep_individual') is True)
        behavior_layout.addWidget(self.keep_box)
        self.individual_row = QWidget()
        individual_form = QFormLayout(self.individual_row)
        individual_form.setContentsMargins(0, 0, 0, 0)
        self.add_directory(individual_form, 'individual_directory', ui_text('器件导出目录'), values)
        behavior_layout.addWidget(self.individual_row)
        model_hint = QLabel(ui_text('勾选后，额外按器件保存独立库与 3D 文件。器件目录留空时使用 PCB 工程所在文件夹；未指定工程时使用已有库所在文件夹。'))
        model_hint.setWordWrap(True)
        behavior_layout.addWidget(model_hint)
        layout.addWidget(behavior)
        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = self.cancel_button = QPushButton(ui_text('取消'))
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = self.save_button = QPushButton(ui_text('确定'))
        save.setObjectName('primary')
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        outer.addLayout(buttons)
        for edit in self.inputs.values():
            edit.textChanged.connect(self.update_controls)
        self.keep_box.toggled.connect(self.update_controls)
        self.update_controls()

    def add_directory(self, form, key, title, values):
        row = QHBoxLayout()
        edit = self.inputs[key] = QLineEdit(values.get(key, ''))
        edit.setAccessibleName(title)
        row.addWidget(edit)
        browse = QPushButton(ui_text('选择…'))
        browse.clicked.connect(lambda: self.choose_directory(edit))
        row.addWidget(browse)
        clear = QPushButton(ui_text('清除'))
        clear.clicked.connect(edit.clear)
        row.addWidget(clear)
        form.addRow(title, row)

    def update_controls(self):
        has_library = any(self.inputs[key].text().strip() for key in ('schlib_target', 'pcblib_target'))
        project = self.inputs['project_path'].text().strip()
        self.import_button.setEnabled(bool(has_library and project) and not self.import_running)
        project_only = bool(project) and not has_library
        self.project_outputs.setVisible(project_only)
        self.project_outputs.setEnabled(project_only)
        self.individual_row.setEnabled(self.keep_box.isChecked())
        default_directory = str(Path(project).parent) if project else ui_text('PCB 工程所在文件夹')
        for kind in ('schlib', 'pcblib'):
            self.inputs[f'project_{kind}_directory'].setPlaceholderText(default_directory)
            self.inputs[f'project_{kind}_name'].setPlaceholderText(Path(project).stem if project else ui_text('PCB 工程名称'))
        self.inputs['project_model_directory'].setPlaceholderText(ui_text('留空：按库名或器件目录自动保存'))
        primary = self.inputs['schlib_target'].text().strip() or self.inputs['pcblib_target'].text().strip()
        self.inputs['individual_directory'].setPlaceholderText(
            default_directory if project else str(Path(primary).parent) if primary else ui_text('已有库所在文件夹'))

    def selected_values(self):
        return {**{key: edit.text().strip() for key, edit in self.inputs.items()},
                'keep_individual': self.keep_box.isChecked()}

    def request_import(self):
        if self.import_button.isEnabled() and not self.import_running:
            self.import_requested.emit({key: self.inputs[key].text().strip() for key in TARGET_PATH_KEYS})

    def set_import_running(self, running):
        self.import_running = running
        self.content.setEnabled(not running)
        self.save_button.setEnabled(not running)
        self.cancel_button.setEnabled(not running)
        self.update_controls()

    def choose(self, edit, suffix):
        self.file_page = choose_path(self, ui_text('选择 .') + suffix, edit.text(), edit.setText,
                                     filter=ui_message('AD 文件 (*.{0})', suffix))

    def choose_directory(self, edit):
        target = next((self.inputs[key].text().strip() for key in ('project_path', 'schlib_target', 'pcblib_target')
                       if self.inputs[key].text().strip()), '')
        initial = edit.text() or (str(Path(target).expanduser().parent) if target else '')
        self.file_page = choose_path(self, ui_text('选择导出目录'), initial,
                                     edit.setText, directory=True)

    @traced('export.append_settings')
    def save(self):
        if self.import_running:
            return
        self.values = self.selected_values()
        try:
            if not any(self.values[key] for key in TARGET_PATH_KEYS):
                raise DownloadError(ui_text('请至少选择一份已有库或一个 PCB 工程'))
            for key, format in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB')):
                if self.values[key]:
                    read_library(self.values[key], format)
            if self.values['project_path']:
                read_project(self.values['project_path'])
            active_keys = []
            if self.project_outputs.isEnabled():
                active_keys.extend(PROJECT_OUTPUT_KEYS)
                for kind in ('schlib', 'pcblib'):
                    name = self.values[f'project_{kind}_name'] or Path(self.values['project_path']).stem
                    library_filename(name, kind.upper())
            if self.keep_box.isChecked():
                active_keys.append('individual_directory')
            for key in active_keys:
                value = self.values[key]
                if key.endswith('_directory') and value:
                    path = Path(value).expanduser().resolve()
                    if path.exists() and not path.is_dir():
                        raise DownloadError(ui_text('导出目录不能是文件：') + value)
        except Exception as exc:
            record_error(exc, 'export.append_settings_invalid', stage='validate')
            self.status.setText(ui_text('无法使用所选文件：') + str(exc))
            return
        log_event('INFO', 'export.append_settings_saved',
                  existing_formats=[fmt for key, fmt in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB'))
                                    if self.values[key]],
                  project_selected=bool(self.values['project_path']),
                  keep_individual=self.values['keep_individual'])
        self.accept()

    def reject(self):
        if self.import_running:
            return
        log_event('DEBUG', 'export.append_settings_cancelled')
        super().reject()
