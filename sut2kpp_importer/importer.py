import re
import tempfile
import traceback
from pathlib import Path

from krita import Extension, Krita
try:                    # Krita 6 (Qt 6)
    from PyQt6.QtCore import QStandardPaths
    from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QLineEdit, QMessageBox)
except ImportError:     # Krita 5 (Qt 5)
    from PyQt5.QtCore import QStandardPaths
    from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QLineEdit, QMessageBox)

from .sut2kpp import __main__ as converter
from .sut2kpp.bundle import write_bundle

ENGINES = (("csp", "CSP Brush engine (closest to Clip Studio)"),
           ("krita", "Krita Pixel and Color Smudge engines"))


def resource_folder():
    """Krita's resource folder: the one set in Settings > Configure Krita > General > Resources, or the default."""
    folder = Krita.instance().readSetting("", "ResourceDirectory", "")
    return Path(folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))


def safe_file_name(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "csp_brushes"


class Options:
    """What the sut2kpp command line would pass to convert_file()."""

    def __init__(self, engine, tag, tip_max, apply_tool_pressure):
        self.engine = engine
        self.tag = tag
        self.tip_max = tip_max
        self.apply_tool_pressure = apply_tool_pressure
        self.keep_tips = False
        self.speed_fast = None
        self.speed_knee = None


class OptionsDialog(QDialog):
    def __init__(self, count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Clip Studio Brushes")
        form = QFormLayout(self)
        self.engine = QComboBox()
        for key, label in ENGINES:
            self.engine.addItem(label, key)
        form.addRow("Convert for:", self.engine)
        self.name = QLineEdit("Clip Studio brushes")
        form.addRow("Bundle name:", self.name)
        self.tag = QLineEdit("imported")
        form.addRow("Tag:", self.tag)
        self.full_tips = QCheckBox("Keep brush tips at full size (larger presets)")
        form.addRow(self.full_tips)
        self.tool_pressure = QCheckBox("Apply the pen pressure graph saved in Clip Studio")
        form.addRow(self.tool_pressure)
        ok, cancel = QDialogButtonBox.StandardButton.Ok, QDialogButtonBox.StandardButton.Cancel
        buttons = QDialogButtonBox(ok | cancel)
        buttons.button(ok).setText("Import %d brush(es)" % count)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def options(self):
        return Options(self.engine.currentData(), self.tag.text().strip(), 0 if self.full_tips.isChecked() else 1024,
                       self.tool_pressure.isChecked())


class SutImporter(Extension):
    def __init__(self, parent):
        super().__init__(parent)

    def setup(self):
        pass

    def createActions(self, window):
        action = window.createAction("sut2kpp_import", "Import Clip Studio Brushes (.sut)...", "tools/scripts")
        action.triggered.connect(self.run)

    def run(self):
        window = Krita.instance().activeWindow()
        parent = window.qwindow() if window else None
        files, _ = QFileDialog.getOpenFileNames(parent, "Clip Studio brushes to import", "",
                                                "Clip Studio sub tools (*.sut)")
        if not files:
            return
        dialog = OptionsDialog(len(files), parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.options()

        entries, failed = [], []
        with tempfile.TemporaryDirectory() as work:
            for file in files:
                try:
                    entries += converter.convert_file(file, Path(work), options)
                except Exception:
                    failed.append("%s:\n%s" % (Path(file).name, traceback.format_exc(limit=1)))
        if not entries:
            QMessageBox.warning(parent, "Import Clip Studio Brushes",
                                "No brush could be converted.\n\n" + "\n".join(failed))
            return

        stem = safe_file_name(dialog.name.text())
        bundle = resource_folder() / (stem + ".bundle")
        number = 1
        while bundle.exists():
            number += 1
            bundle = bundle.with_name("%s_%d.bundle" % (stem, number))
        write_bundle(bundle, entries, title=dialog.name.text(), tags=[options.tag] if options.tag else [])

        text = "%d brush(es) written to\n%s\n\nRestart Krita to see them" % (len(files) - len(failed), bundle)
        if options.tag:
            text += ' (tag "%s")' % options.tag
        text += "."
        if failed:
            text += "\n\nNot converted:\n" + "\n".join(failed)
        QMessageBox.information(parent, "Import Clip Studio Brushes", text)
