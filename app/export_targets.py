"""Library append destinations and Altium project integration controls."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

from altium_project import add_libraries_to_project, read_project
from library_merge import read_library


class ExportTargetsDialog(QDialog):
    def __init__(self, values, parent):
        super().__init__(parent)
        self.setWindowTitle('元件库与 PCB 工程')
        self.setWindowModality(Qt.WindowModal)
        self.resize(660, 350)
        layout = QVBoxLayout(self)
        hint = QLabel('指定已有库后，将所选元件追加到该库；留空则按主页名称生成合并库。\n同名条目保留原内容；修改已有库和工程前会自动备份到应用数据目录。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.inputs = {}
        for key, title, suffix in (('schlib_target', '已有符号库', 'SchLib'),
                                    ('pcblib_target', '已有封装库', 'PcbLib'),
                                    ('project_path', 'PCB 工程', 'PrjPcb')):
            row = QHBoxLayout()
            edit = self.inputs[key] = QLineEdit(values.get(key, ''))
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
        self.project_box = QCheckBox('下载完成后，将成功导出的库加入此工程')
        self.project_box.setChecked(bool(values.get('project_path')))
        layout.addWidget(self.project_box)
        project_hint = QLabel('工程中保存库的文件引用；已打开的 AD 工程需重新加载后查看。')
        project_hint.setWordWrap(True)
        layout.addWidget(project_hint)
        self.status = QLabel('')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.import_button = QPushButton('立即导入已有库到工程…')
        self.import_button.clicked.connect(self.import_libraries)
        buttons.addWidget(self.import_button)
        buttons.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton('确定')
        save.setObjectName('primary')
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def choose(self, edit, suffix):
        path, _ = QFileDialog.getOpenFileName(self, '选择 .' + suffix, edit.text(), f'AD 文件 (*.{suffix})')
        if path:
            edit.setText(path)
            if suffix == 'PrjPcb':
                self.project_box.setChecked(True)

    def save(self):
        self.values = {key: edit.text().strip() for key, edit in self.inputs.items()}
        if not self.project_box.isChecked():
            self.values['project_path'] = ''
        try:
            for key, format in (('schlib_target', 'SCHLIB'), ('pcblib_target', 'PCBLIB')):
                if self.values[key]:
                    read_library(self.values[key], format)
            if self.project_box.isChecked():
                read_project(self.values['project_path'])
        except Exception as exc:
            self.status.setText('无法使用所选文件：' + str(exc))
            return
        self.accept()

    def import_libraries(self):
        project = self.inputs['project_path'].text().strip()
        if not project:
            self.choose(self.inputs['project_path'], 'PrjPcb')
            project = self.inputs['project_path'].text().strip()
        if not project:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, '选择要加入工程的库', str(Path(project).parent),
                                              'AD 元件库 (*.SchLib *.PcbLib)')
        if not paths:
            return
        try:
            for path in paths:
                read_library(path, Path(path).suffix[1:].upper())
            result = add_libraries_to_project(project, paths)
            self.status.setText(f"工程导入完成：新增 {result['added']} 份库，跳过 {result['skipped']} 份已有引用。")
        except Exception as exc:
            self.status.setText('工程导入失败：' + str(exc))
