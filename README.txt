Feature Datasets Manager (v0.3.0)

Install: QGIS > Plugins > Manage and Install Plugins > Install from ZIP.
Open:    Plugins > Feature Datasets Manager (or the toolbar icon).

1. Choose the storage format: GeoPackage, SpatiaLite or File Geodatabase.
2. Open an existing file or name a new one.
3. Work with feature datasets:
   - Create Dataset...  set the CRS once.
   - Edit Dataset...    rename, change the CRS (optionally reproject its layers), description.
   - Delete Dataset...  remove a dataset (optionally with its layers).
   - New Layer...       the layer takes the dataset CRS automatically.
   - Add Existing Layer...  CRS is checked; reprojected copy when needed.
   - Validate...        reports layers whose CRS differs from their dataset.

GeoPackage / SpatiaLite: dataset definitions live in two helper tables
(feature_datasets, feature_dataset_members) inside the file; other software ignores them.
File Geodatabase: real feature datasets are used (needs GDAL 3.6+, i.e. QGIS 3.28+). GDAL cannot
create an empty feature dataset, so a new dataset is remembered in a small .json file next to the
.gdb and appears inside the geodatabase when its first layer is created. A layer lives in one
dataset only, so "Add Existing Layer" copies it into the dataset.

Close edit mode on the file's layers before creating layers in it.

License: GNU GPL v2 (see LICENSE). Author: Belal Mahmoud Abdelmonem.
