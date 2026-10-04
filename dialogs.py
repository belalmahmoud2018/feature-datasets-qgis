"""Dialogs for the Feature Datasets plugin."""
import contextlib

from qgis.core import QgsProject, QgsVectorLayer
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from . import db

GPKG_FILTER = "GeoPackage (*.gpkg)"


class _GpkgPicker(QHBoxLayout):
    """Line edit + Browse button."""

    def __init__(self, parent, allow_new=False):
        super().__init__()
        self.parent_widget = parent
        self.allow_new = allow_new
        self.edit = QLineEdit()
        self.btn = QPushButton("Browse...")
        self.addWidget(self.edit, 1)
        self.addWidget(self.btn)
        self.btn.clicked.connect(self._browse)

    def _browse(self):
        if self.allow_new:
            opt = getattr(QFileDialog, "DontConfirmOverwrite", None)
            if opt is None:
                opt = QFileDialog.Option.DontConfirmOverwrite
            path, _ = QFileDialog.getSaveFileName(
                self.parent_widget, "GeoPackage (existing or new)", self.edit.text(), GPKG_FILTER,
                options=opt,
            )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self.parent_widget, "Select GeoPackage", self.edit.text(), GPKG_FILTER
            )
        if path:
            if self.allow_new and not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.edit.setText(path)

    def path(self):
        return self.edit.text().strip()


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


def _load_layer(path, name):
    layer = QgsVectorLayer("%s|layername=%s" % (path, name), name, "ogr")
    if layer.isValid():
        QgsProject.instance().addMapLayer(layer)
    return layer.isValid()


# ------------------------------------------------------- Create dataset
class CreateDatasetDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Create Feature Dataset")
        self.setMinimumWidth(480)
        form = QFormLayout()
        self.picker = _GpkgPicker(self, allow_new=True)
        form.addRow("GeoPackage:", self.picker)
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
        path = self.picker.path()
        crs = self.crs.crs()
        try:
            if not path:
                raise db.DatasetError("Please select a GeoPackage file.")
            if not crs.isValid():
                raise db.DatasetError("Please choose a valid CRS.")
            db.create_dataset(path, self.name.text(), crs.authid(), crs.toWkt(), self.desc.text())
        except db.DatasetError as e:
            QMessageBox.warning(self, "Feature Datasets", str(e))
            return
        except Exception as e:  # unexpected
            QMessageBox.critical(self, "Feature Datasets", "Unexpected error:\n%s" % e)
            return
        self.iface.messageBar().pushSuccess(
            "Feature Datasets",
            "Dataset '%s' created with CRS %s." % (self.name.text().strip(), crs.authid()),
        )
        super().accept()


# ----------------------------------------------- shared dataset chooser
class _DatasetMixin:
    def _build_dataset_row(self, form):
        self.picker = _GpkgPicker(self)
        form.addRow("GeoPackage:", self.picker)
        self.ds_combo = QComboBox()
        form.addRow("Feature dataset:", self.ds_combo)
        self.crs_label = QLabel("-")
        form.addRow("Dataset CRS:", self.crs_label)
        self.picker.edit.textChanged.connect(self._reload_datasets)
        self.ds_combo.currentIndexChanged.connect(self._dataset_changed)

    def _reload_datasets(self):
        self.ds_combo.blockSignals(True)
        self.ds_combo.clear()
        self._datasets = {}
        path = self.picker.path()
        with contextlib.suppress(Exception):
            for d in db.list_datasets(path):
                self._datasets[d["name"]] = d
                self.ds_combo.addItem(d["name"])
        self.ds_combo.blockSignals(False)
        self._dataset_changed()

    def _dataset_changed(self):
        d = self._datasets.get(self.ds_combo.currentText())
        self.crs_label.setText(
            db.describe_srs(db.make_srs(d["authid"], d["wkt"])) if d else "-"
        )
        self._after_dataset_changed()

    def _after_dataset_changed(self):
        pass


# --------------------------------------------------------- New layer
class NewLayerDialog(QDialog, _DatasetMixin):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._datasets = {}
        self.setWindowTitle("New Layer in Feature Dataset")
        self.setMinimumWidth(520)
        form = QFormLayout()
        self._build_dataset_row(form)
        self.name = QLineEdit()
        form.addRow("Layer name:", self.name)
        self.geom = QComboBox()
        for label, _ in db.GEOM_TYPES:
            self.geom.addItem(label)
        form.addRow("Geometry type:", self.geom)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Field name", "Type"])
        self.table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("Add field")
        rem = QPushButton("Remove selected field")
        add.clicked.connect(self._add_field)
        rem.clicked.connect(self._remove_field)
        brow = QHBoxLayout()
        brow.addWidget(add)
        brow.addWidget(rem)
        brow.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("Fields:"))
        lay.addWidget(self.table)
        lay.addLayout(brow)
        lay.addLayout(_buttons(self, "Create layer"))

    def _add_field(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, self._item(""))
        combo = QComboBox()
        for label, _ in db.FIELD_TYPES:
            combo.addItem(label)
        self.table.setCellWidget(r, 1, combo)

    @staticmethod
    def _item(text):
        from qgis.PyQt.QtWidgets import QTableWidgetItem

        return QTableWidgetItem(text)

    def _remove_field(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def accept(self):
        path = self.picker.path()
        dataset = self.ds_combo.currentText()
        try:
            if not dataset:
                raise db.DatasetError(
                    "No feature dataset selected. Create one first (or pick a file that has one)."
                )
            fields = []
            for r in range(self.table.rowCount()):
                fname = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
                if not fname:
                    raise db.DatasetError("Row %d: field name is empty." % (r + 1))
                ftype = db.FIELD_TYPES[self.table.cellWidget(r, 1).currentIndex()][1]
                fields.append((fname, ftype))
            gtype = db.GEOM_TYPES[self.geom.currentIndex()][1]
            name = db.create_layer(path, dataset, self.name.text(), gtype, fields)
        except db.DatasetError as e:
            QMessageBox.warning(self, "Feature Datasets", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets", "Unexpected error:\n%s" % e)
            return
        if not _load_layer(path, name):
            QMessageBox.information(
                self, "Feature Datasets", "Layer created, but it could not be added to the project."
            )
        self.iface.messageBar().pushSuccess(
            "Feature Datasets", "Layer '%s' created in dataset '%s'." % (name, dataset)
        )
        super().accept()


# --------------------------------------------------------- Add layer
class AddLayerDialog(QDialog, _DatasetMixin):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._datasets = {}
        self._tables = {}
        self._match = True
        self.setWindowTitle("Add Existing Layer to Feature Dataset")
        self.setMinimumWidth(520)
        form = QFormLayout()
        self._build_dataset_row(form)
        self.table_combo = QComboBox()
        form.addRow("Layer in the file:", self.table_combo)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        form.addRow("CRS check:", self.status)
        self.reproject = QCheckBox("Create a reprojected copy in the dataset CRS")
        self.reproject.setVisible(False)
        form.addRow("", self.reproject)
        self.table_combo.currentIndexChanged.connect(self._check)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(_buttons(self, "Add"))

    def _after_dataset_changed(self):
        self.table_combo.blockSignals(True)
        self.table_combo.clear()
        self._tables = {}
        path, dataset = self.picker.path(), self.ds_combo.currentText()
        if dataset and db.is_geopackage(path):
            with contextlib.suppress(Exception):
                members = set(db.list_members(path, dataset))
                for t in db.list_tables(path):
                    if t["name"] not in members:
                        self._tables[t["name"]] = t
                        self.table_combo.addItem(t["name"])
        self.table_combo.blockSignals(False)
        self._check()

    def _check(self):
        self.reproject.setVisible(False)
        self.reproject.setChecked(False)
        self._match = True
        t = self._tables.get(self.table_combo.currentText())
        d = self._datasets.get(self.ds_combo.currentText())
        if not t or not d:
            self.status.setText("")
            return
        if not t["has_geom"]:
            self.status.setText("No geometry - nothing to check.")
            return
        dsrs = db.make_srs(d["authid"], d["wkt"])
        if db.same_crs(t["srs"], dsrs):
            self.status.setText("OK - same CRS (%s)." % db.describe_srs(dsrs))
        else:
            self._match = False
            self.status.setText(
                "Different CRS: layer is %s, dataset is %s."
                % (db.describe_srs(t["srs"]), db.describe_srs(dsrs))
            )
            self.reproject.setVisible(True)

    def accept(self):
        path = self.picker.path()
        dataset = self.ds_combo.currentText()
        table = self.table_combo.currentText()
        try:
            if not dataset or not table:
                raise db.DatasetError("Choose a file, a dataset and a layer first.")
            if self._match:
                db.add_member(path, dataset, table)
                msg = "Layer '%s' added to dataset '%s'." % (table, dataset)
            elif self.reproject.isChecked():
                d = self._datasets[dataset]
                code = (d["authid"] or "").replace(":", "") or "reproj"
                new = db.reproject_copy(path, table, "%s_%s" % (table, code), dataset)
                _load_layer(path, new)
                msg = "Reprojected copy '%s' created and added to '%s'." % (new, dataset)
            else:
                raise db.DatasetError(
                    "The layer's CRS differs from the dataset CRS. Tick the reprojection "
                    "option, or reproject the layer yourself first."
                )
        except db.DatasetError as e:
            QMessageBox.warning(self, "Feature Datasets", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Feature Datasets", "Unexpected error:\n%s" % e)
            return
        self.iface.messageBar().pushSuccess("Feature Datasets", msg)
        super().accept()


# ---------------------------------------------------------- Validate
class ValidateDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Validate Feature Datasets")
        self.setMinimumSize(560, 420)
        self.picker = _GpkgPicker(self)
        run = QPushButton("Validate")
        run.clicked.connect(self._run)
        top = QHBoxLayout()
        top.addLayout(self.picker, 1)
        top.addWidget(run)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.out)
        lay.addWidget(close)

    def _run(self):
        path = self.picker.path()
        if not db.is_geopackage(path):
            self.out.setPlainText("Please select a valid GeoPackage file.")
            return
        try:
            lines, problems = db.validate(path)
        except Exception as e:
            self.out.setPlainText("Error: %s" % e)
            return
        lines.append("")
        if problems:
            lines.append("Result: %d problem(s) found." % problems)
        else:
            lines.append("Result: all layers match their dataset CRS.")
        self.out.setPlainText("\n".join(lines))
