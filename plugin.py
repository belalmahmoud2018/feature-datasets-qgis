try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5

from .dialogs import AddLayerDialog, CreateDatasetDialog, NewLayerDialog, ValidateDialog

MENU = "&Feature Datasets"


class FeatureDatasetsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []

    def _add(self, text, callback):
        action = QAction(text, self.iface.mainWindow())
        action.triggered.connect(lambda _checked=False: callback())
        self.iface.addPluginToMenu(MENU, action)
        self.actions.append(action)

    def initGui(self):
        self._add("Create Feature Dataset...", lambda: self._open(CreateDatasetDialog))
        self._add("New Layer in Feature Dataset...", lambda: self._open(NewLayerDialog))
        self._add("Add Existing Layer to Feature Dataset...", lambda: self._open(AddLayerDialog))
        self._add("Validate Feature Datasets...", lambda: self._open(ValidateDialog))

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
        self.actions = []

    def _open(self, dialog_cls):
        dlg = dialog_cls(self.iface, self.iface.mainWindow())
        dlg.exec()
