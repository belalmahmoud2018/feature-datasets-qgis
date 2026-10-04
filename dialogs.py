"""Dialogs for the Feature Datasets Manager plugin."""
import contextlib

from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsVectorLayer
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import db


def _dont_confirm():
    opt = getattr(QFileDialog, "DontConfirmOverwrite", None)
    if opt is None:
        opt = QFileDialog.Option.DontConfirmOverwrite
    return opt


def _load_layer(backend, path, name):
    layer = QgsVectorLayer(backend.uri(path, name), name, "ogr")
    if layer.isValid():
        QgsProject.instance().addMapLayer(layer)
    return layer.isValid()


def _norm(p):
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _loaded_layers(path, members):
    """Project layers that come from `path` and belong to one of `members`."""
    out = []
    members = set(members)
    for layer in QgsProject.instance().mapLayers().values():
        src = layer.source() if hasattr(layer, "source") else ""
        parts = src.split("|")
        if _norm(parts[0]) != _norm(path):
            continue
        name = ""
        for p in parts[1:]:
            if p.lower().startswith("layername="):
                name = p.split("=", 1)[1]
        if name in members:
            out.append(layer)
    return out


def _remove_from_project(layers):
    QgsProject.instance().removeMapLayers([lyr.id() for lyr in layers])


def _crs_from_row(row):
    crs = QgsCoordinateReferenceSystem()
    if row.get("authid"):
        crs = QgsCoordinateReferenceSystem(row["authid"])
    if not crs.isValid() and row.get("wkt"):
        crs = QgsCoordinateReferenceSystem.fromWkt(row["wkt"])
    return crs


def _buttons(dialog, ok_text="OK"):
    row = QHBoxLayout()
    row.addStretch(1)
    ok = QPushButton(ok_text)
    cancel = QPushButton("Cancel")
    ok.setDefault(True)
    row.addWidget(ok)
    row.addWidget(cancel)
    ok.clicked.connect(dialog.accept)
    cancel.clicked.connect(dialog.reject)
    return row


def _warn(parent, text):
    QMessageBox.warning(parent, "Feature Datasets Manager", text)


# ------------------------------------------------------ reusable widgets
class FieldsEditor(QWidget):
    def __init__(self, field_types, parent=None):
        super().__init__(parent)
        self.field_types = field_types
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Field name", "Type"])
        self.table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("Add field")
        rem = QPushButton("Remove selected field")
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Fields:"))
        lay.addWidget(self.table)
        lay.addLayout(row)

    def _add(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(""))
        combo = QComboBox()
        for label, _ in self.field_types:
            combo.addItem(label)
        self.table.setCellWidget(r, 1, combo)

    def _remove(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def fields(self):
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            fname = (item.text() if item else "").strip()
            if not fname:
                raise db.DatasetError("Row %d: field name is empty." % (r + 1))
            ftype = self.field_types[self.table.cellWidget(r, 1).currentIndex()][1]
            out.append((fname, ftype))
        return out


class LayerSpec(QWidget):
    """Layer name + geometry type + fields."""

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.name = QLineEdit()
        self.geom = QComboBox()
        for label, _ in backend.geom_types():
            self.geom.addItem(label)
        self.fields_editor = FieldsEditor(backend.field_types())
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Layer name:", self.name)
        form.addRow("Geometry type:", self.geom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(form)
        lay.addWidget(self.fields_editor)

    def spec(self):
        gtype = self.backend.geom_types()[self.geom.currentIndex()][1]
        return self.name.text().strip(), gtype, self.fields_editor.fields()


# ------------------------------------------------------- Create dataset
class CreateDatasetDialog(QDialog):
    def __init__(self, iface, backend, path, parent=None):
        super().__init__(parent)
        self.iface, self.backend, self.path = iface, backend, path
        self.setWindowTitle("Create Feature Dataset - " + backend.label)
        self.setMinimumWidth(520)
        form = QFormLayout()
        form.addRow("File:", QLabel(path))
        self.name = QLineEdit()
        form.addRow("Dataset name:", self.name)
        self.crs = QgsProjectionSelectionWidget()
        self.crs.setCrs(QgsProject.instance().crs())
        form.addRow("CRS (set once):", self.crs)
        self.desc = QLineEdit()
        form.addRow("Description:", self.desc)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(_buttons(self, "Create"))

    def accept(self):
        crs = self.crs.crs()
        try:
            if not crs.isValid():
                raise db.DatasetError("Please choose a valid CRS.")
            self.backend.create_dataset(
                self.path, self.name.text(), crs.authid(), crs.toWkt(), self.desc.text()
            )
        except db.DatasetError as e:
            _warn(self, str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets Manager", "Unexpected error:\n%s" % e)
            return
        self.iface.messageBar().pushSuccess(
            "Feature Datasets Manager",
            "Dataset '%s' created with CRS %s." % (self.name.text().strip(), crs.authid()),
        )
        super().accept()


# -------------------------------------------------------- Edit dataset
class EditDatasetDialog(QDialog):
    def __init__(self, iface, backend, path, name, parent=None):
        super().__init__(parent)
        self.iface, self.backend, self.path, self.old_name = iface, backend, path, name
        self.setWindowTitle("Edit Feature Dataset - " + backend.label)
        self.setMinimumWidth(540)
        row = backend.get_dataset(path, name) or {}
        self.caps = backend.edit_capabilities(path, name)
        self.members = backend.list_members(path, name)
        form = QFormLayout()
        form.addRow("File:", QLabel(path))
        self.name = QLineEdit(name)
        self.name.setEnabled(self.caps["rename"])
        form.addRow("Dataset name:", self.name)
        self.crs = QgsProjectionSelectionWidget()
        crs = _crs_from_row(row)
        if crs.isValid():
            self.crs.setCrs(crs)
        self.crs.setEnabled(self.caps["crs"])
        form.addRow("CRS:", self.crs)
        self.desc = QLineEdit(row.get("description") or "")
        self.desc.setEnabled(self.caps["description"])
        form.addRow("Description:", self.desc)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        self.reproject = QCheckBox(
            "Also reproject the %d layer(s) of this dataset to the new CRS\n"
            "(a backup copy of the file is made first)" % len(self.members)
        )
        self.reproject.setVisible(self.caps["reproject"] and bool(self.members))
        lay.addWidget(self.reproject)
        if self.caps["note"]:
            note = QLabel(self.caps["note"])
            note.setWordWrap(True)
            lay.addWidget(note)
        lay.addLayout(_buttons(self, "Save"))

    def accept(self):
        crs = self.crs.crs()
        do_reproject = self.reproject.isVisible() and self.reproject.isChecked()
        try:
            if do_reproject:
                loaded = _loaded_layers(self.path, self.members)
                if loaded:
                    names = ", ".join(lyr.name() for lyr in loaded)
                    dlg = ConfirmDialog(
                        "Reproject layers",
                        "These layers are loaded in the QGIS project and will be removed from it "
                        "so the file can be updated:\n\n%s\n\nContinue?" % names,
                        "Continue",
                        self,
                    )
                    if not dlg.exec():
                        return
                    _remove_from_project(loaded)
            msg = self.backend.edit_dataset(
                self.path, self.old_name, self.name.text().strip(), crs.authid(), crs.toWkt(),
                self.desc.text(), do_reproject,
            )
        except db.DatasetError as e:
            _warn(self, str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets Manager", "Unexpected error:\n%s" % e)
            return
        QMessageBox.information(self, "Feature Datasets Manager", msg)
        super().accept()


# -------------------------------------------------------- Delete dataset
class ConfirmDialog(QDialog):
    def __init__(self, title, text, ok_text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        label = QLabel(text)
        label.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(label)
        lay.addLayout(_buttons(self, ok_text))


class DeleteDatasetDialog(QDialog):
    def __init__(self, backend, path, name, parent=None):
        super().__init__(parent)
        self.backend, self.path, self.name = backend, path, name
        self.members = backend.list_members(path, name)
        self.setWindowTitle("Delete Feature Dataset - " + backend.label)
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        text = "Delete the feature dataset '%s'?" % name
        if self.members:
            text += "\n\nLayers in it: " + ", ".join(self.members)
        label = QLabel(text)
        label.setWordWrap(True)
        lay.addWidget(label)
        self.with_layers = QCheckBox("Also delete these %d layer(s) from the file (permanent)" % len(self.members))
        if backend.is_directory:
            note = QLabel(
                "In a File Geodatabase the layers belong to the dataset, so they are deleted with it."
            )
            note.setWordWrap(True)
            lay.addWidget(note)
        elif self.members:
            lay.addWidget(self.with_layers)
        else:
            self.with_layers.setVisible(False)
        lay.addLayout(_buttons(self, "Delete"))

    def delete_layers(self):
        return self.backend.is_directory or self.with_layers.isChecked()

    def accept(self):
        delete_layers = self.delete_layers()
        try:
            if delete_layers and self.members:
                loaded = _loaded_layers(self.path, self.members)
                if loaded:
                    names = ", ".join(lyr.name() for lyr in loaded)
                    dlg = ConfirmDialog(
                        "Delete layers",
                        "These layers are loaded in the QGIS project and will be removed from it:\n\n%s"
                        % names,
                        "Continue",
                        self,
                    )
                    if not dlg.exec():
                        return
                    _remove_from_project(loaded)
            msg = self.backend.delete_dataset(self.path, self.name, delete_layers)
        except db.DatasetError as e:
            _warn(self, str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets Manager", "Unexpected error:\n%s" % e)
            return
        QMessageBox.information(self, "Feature Datasets Manager", msg)
        super().accept()


# ------------------------------------------ shared dataset chooser logic
class _DatasetChooser:
    def _load_datasets(self, combo, preselect):
        self._datasets = {}
        combo.blockSignals(True)
        combo.clear()
        with contextlib.suppress(Exception):
            for d in self.backend.list_datasets(self.path):
                self._datasets[d["name"]] = d
                combo.addItem(d["name"])
        if preselect:
            idx = combo.findText(preselect)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _crs_text(self, name):
        d = self._datasets.get(name)
        if not d or not d.get("wkt"):
            return "unknown (no layers yet)" if d else "-"
        return db.describe_srs(db.make_srs(d["authid"], d["wkt"]))


# --------------------------------------------------------- New layer
class NewLayerDialog(QDialog, _DatasetChooser):
    def __init__(self, iface, backend, path, preselect="", parent=None):
        super().__init__(parent)
        self.iface, self.backend, self.path = iface, backend, path
        self.setWindowTitle("New Layer in Feature Dataset - " + backend.label)
        self.setMinimumWidth(540)
        self.ds_combo = QComboBox()
        self.crs_label = QLabel("-")
        form = QFormLayout()
        form.addRow("Feature dataset:", self.ds_combo)
        form.addRow("Dataset CRS:", self.crs_label)
        self.spec = LayerSpec(backend)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.spec)
        lay.addLayout(_buttons(self, "Create layer"))
        self._load_datasets(self.ds_combo, preselect)
        self.ds_combo.currentIndexChanged.connect(self._changed)
        self._changed()

    def _changed(self):
        self.crs_label.setText(self._crs_text(self.ds_combo.currentText()))

    def accept(self):
        dataset = self.ds_combo.currentText()
        try:
            if not dataset:
                raise db.DatasetError("No feature dataset selected. Create one first.")
            name, gtype, fields = self.spec.spec()
            name = self.backend.create_layer(self.path, dataset, name, gtype, fields)
        except db.DatasetError as e:
            _warn(self, str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets Manager", "Unexpected error:\n%s" % e)
            return
        if not _load_layer(self.backend, self.path, name):
            QMessageBox.information(
                self, "Feature Datasets Manager",
                "Layer created, but it could not be added to the project.",
            )
        self.iface.messageBar().pushSuccess(
            "Feature Datasets Manager", "Layer '%s' created in dataset '%s'." % (name, dataset)
        )
        super().accept()


# --------------------------------------------------------- Add layer
class AddLayerDialog(QDialog, _DatasetChooser):
    def __init__(self, iface, backend, path, preselect="", parent=None):
        super().__init__(parent)
        self.iface, self.backend, self.path = iface, backend, path
        self._tables = {}
        self._needs_copy = backend.is_directory
        self.setWindowTitle("Add Existing Layer to Feature Dataset - " + backend.label)
        self.setMinimumWidth(540)
        self.ds_combo = QComboBox()
        self.crs_label = QLabel("-")
        self.table_combo = QComboBox()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.new_name = QLineEdit()
        form = QFormLayout()
        form.addRow("Feature dataset:", self.ds_combo)
        form.addRow("Dataset CRS:", self.crs_label)
        form.addRow("Layer to add:", self.table_combo)
        form.addRow("CRS check:", self.status)
        form.addRow("New layer name (for the copy):", self.new_name)
        self.copy_btn = QPushButton("Add a reprojected copy instead")
        self.copy_btn.setVisible(False)
        self.copy_btn.clicked.connect(self._use_copy)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.copy_btn)
        lay.addLayout(_buttons(self, "Add"))
        self._load_datasets(self.ds_combo, preselect)
        self.ds_combo.currentIndexChanged.connect(self._dataset_changed)
        self.table_combo.currentIndexChanged.connect(self._check)
        self._dataset_changed()

    def _dataset_changed(self):
        name = self.ds_combo.currentText()
        self.crs_label.setText(self._crs_text(name))
        self.table_combo.blockSignals(True)
        self.table_combo.clear()
        self._tables = {}
        if name:
            with contextlib.suppress(Exception):
                for t in self.backend.candidate_layers(self.path, name):
                    self._tables[t["name"]] = t
                    self.table_combo.addItem(t["name"])
        self.table_combo.blockSignals(False)
        self._check()

    def _default_copy_name(self):
        table = self.table_combo.currentText()
        d = self._datasets.get(self.ds_combo.currentText())
        code = ((d or {}).get("authid") or "").replace(":", "") or self.ds_combo.currentText()
        if self.backend.is_directory:
            return "%s_%s" % (table, self.ds_combo.currentText())
        return "%s_%s" % (table, code)

    def _check(self):
        self.copy_btn.setVisible(False)
        self._needs_copy = self.backend.is_directory
        t = self._tables.get(self.table_combo.currentText())
        d = self._datasets.get(self.ds_combo.currentText())
        self.new_name.setText(self._default_copy_name() if t else "")
        self.new_name.setEnabled(self._needs_copy)
        if not t or not d:
            self.status.setText("")
            return
        if self.backend.is_directory:
            self.status.setText(
                "A layer can live in one feature dataset only, so it is copied into '%s' "
                "(reprojected if needed). The original is not changed." % d["name"]
            )
            return
        if not t["has_geom"]:
            self.status.setText("No geometry - nothing to check.")
            return
        dsrs = self.backend.dataset_srs(d)
        if db.same_crs(t["srs"], dsrs):
            self.status.setText("OK - same CRS (%s)." % db.describe_srs(dsrs))
        else:
            self.status.setText(
                "Different CRS: layer is %s, dataset is %s."
                % (db.describe_srs(t["srs"]), db.describe_srs(dsrs))
            )
            self.copy_btn.setVisible(True)

    def _use_copy(self):
        self._needs_copy = True
        self.new_name.setEnabled(True)
        self.status.setText(
            self.status.text() + "  -> A reprojected copy will be created; press Add to continue."
        )
        self.copy_btn.setVisible(False)

    def accept(self):
        dataset = self.ds_combo.currentText()
        table = self.table_combo.currentText()
        mismatch = self.copy_btn.isVisible()
        try:
            if not dataset or not table:
                raise db.DatasetError("Choose a dataset and a layer first.")
            if mismatch:
                raise db.DatasetError(
                    "The layer's CRS differs from the dataset CRS. Press 'Add a reprojected copy "
                    "instead', or reproject the layer yourself first."
                )
            if self._needs_copy:
                name = self.backend.add_existing(
                    self.path, dataset, table, self.new_name.text().strip(), True
                )
                _load_layer(self.backend, self.path, name)
                msg = "Layer '%s' created in dataset '%s'." % (name, dataset)
            else:
                self.backend.add_existing(self.path, dataset, table)
                msg = "Layer '%s' added to dataset '%s'." % (table, dataset)
        except db.DatasetError as e:
            _warn(self, str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets Manager", "Unexpected error:\n%s" % e)
            return
        self.iface.messageBar().pushSuccess("Feature Datasets Manager", msg)
        super().accept()


# ---------------------------------------------------------- Validate
class ValidateDialog(QDialog):
    def __init__(self, backend, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Validate Feature Datasets - " + backend.label)
        self.setMinimumSize(560, 420)
        out = QPlainTextEdit()
        out.setReadOnly(True)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(out)
        lay.addWidget(close)
        try:
            lines, problems = backend.validate(path)
        except Exception as e:
            out.setPlainText("Error: %s" % e)
            return
        lines.append("")
        if problems:
            lines.append("Result: %d problem(s) found." % problems)
        else:
            lines.append("Result: all layers match their dataset CRS.")
        out.setPlainText("\n".join(lines))


# --------------------------------------------------------- Main window
class HubDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.backend = db.BACKENDS[0]
        self._names = []
        self.setWindowTitle("Feature Datasets Manager")
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("<b>1. Choose the storage format</b>"))
        self.radios = []
        for b in db.BACKENDS:
            r = QRadioButton(b.label)
            r.toggled.connect(lambda checked, bk=b: self._format_changed(checked, bk))
            lay.addWidget(r)
            self.radios.append(r)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        lay.addWidget(QLabel("<b>2. Choose the file</b>"))
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.editingFinished.connect(self._refresh)
        open_btn = QPushButton("Open existing...")
        new_btn = QPushButton("New...")
        open_btn.clicked.connect(self._open_existing)
        new_btn.clicked.connect(self._new)
        row.addWidget(self.path_edit, 1)
        row.addWidget(open_btn)
        row.addWidget(new_btn)
        lay.addLayout(row)

        lay.addWidget(QLabel("<b>3. Feature datasets in the file</b>"))
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        lay.addWidget(self.list)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        for group in (
            (("Create Dataset...", self._create), ("Edit Dataset...", self._edit),
             ("Delete Dataset...", self._delete)),
            (("New Layer...", self._new_layer), ("Add Existing Layer...", self._add_layer),
             ("Validate...", self._validate)),
        ):
            actions = QHBoxLayout()
            for text, fn in group:
                btn = QPushButton(text)
                btn.clicked.connect(fn)
                actions.addWidget(btn)
            lay.addLayout(actions)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        lay.addWidget(close)

        self.radios[0].setChecked(True)

    # -- format / file selection
    def _format_changed(self, checked, backend):
        if not checked:
            return
        self.backend = backend
        self.hint.setText(backend.hint)
        self.path_edit.clear()
        self._refresh()

    def _open_existing(self):
        b = self.backend
        start = self.path_edit.text()
        if b.is_directory:
            path = QFileDialog.getExistingDirectory(self, "Select the .gdb folder", start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select file", start, b.open_filter)
        if path:
            self.path_edit.setText(path)
            self._refresh()

    def _new(self):
        b = self.backend
        path, _ = QFileDialog.getSaveFileName(
            self, "New file", self.path_edit.text(), b.save_filter, options=_dont_confirm()
        )
        if path:
            self.path_edit.setText(b.normalize_path(path))
            self._refresh()

    def _refresh(self):
        self.list.clear()
        self._names = []
        self.status.setText("")
        path = self.path_edit.text().strip()
        if not path or not self.backend.is_valid(path):
            if path:
                self.status.setText(
                    "File not found or not a valid %s. Use 'Create Dataset...' to make a new one."
                    % self.backend.label
                )
            return
        try:
            datasets = self.backend.list_datasets(path)
            for d in datasets:
                srs = self.backend.dataset_srs(d)
                count = len(self.backend.list_members(path, d["name"]))
                pending = "    (no layers yet)" if d.get("pending") else ""
                self.list.addItem(
                    "%s    [%s]    %d layer(s)%s" % (d["name"], db.describe_srs(srs), count, pending)
                )
                self._names.append(d["name"])
            if not datasets:
                self.status.setText("No feature datasets in this file yet.")
        except Exception as e:
            self.status.setText("Could not read the file: %s" % e)

    def _selected(self):
        row = self.list.currentRow()
        return self._names[row] if 0 <= row < len(self._names) else ""

    def _existing(self):
        path = self.path_edit.text().strip()
        if not self.backend.is_valid(path):
            _warn(self, "Choose an existing %s file first." % self.backend.label)
            return None
        return path

    # -- actions
    def _create(self):
        path = self.backend.normalize_path(self.path_edit.text())
        if not path:
            _warn(self, "Choose or name the file first.")
            return
        self.path_edit.setText(path)
        CreateDatasetDialog(self.iface, self.backend, path, self).exec()
        self._refresh()

    def _need_selection(self):
        name = self._selected()
        if not name:
            _warn(self, "Select a dataset from the list first.")
        return name

    def _edit(self):
        path = self._existing()
        name = self._need_selection() if path else ""
        if path and name:
            EditDatasetDialog(self.iface, self.backend, path, name, self).exec()
            self._refresh()

    def _delete(self):
        path = self._existing()
        name = self._need_selection() if path else ""
        if path and name:
            DeleteDatasetDialog(self.backend, path, name, self).exec()
            self._refresh()

    def _new_layer(self):
        path = self._existing()
        if path:
            NewLayerDialog(self.iface, self.backend, path, self._selected(), self).exec()
            self._refresh()

    def _add_layer(self):
        path = self._existing()
        if path:
            AddLayerDialog(self.iface, self.backend, path, self._selected(), self).exec()
            self._refresh()

    def _validate(self):
        path = self._existing()
        if path:
            ValidateDialog(self.backend, path, self).exec()
