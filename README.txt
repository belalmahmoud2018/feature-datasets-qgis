Feature Datasets for GeoPackage (v0.1.0, experimental)

Install: QGIS > Plugins > Manage and Install Plugins > Install from ZIP.
Menu:    Plugins > Feature Datasets

1. Create Feature Dataset...   pick/create a .gpkg, name the dataset, choose its CRS once.
2. New Layer in Feature Dataset...  layer is created with the dataset CRS automatically.
3. Add Existing Layer to Feature Dataset...  CRS is checked; optional reprojected copy.
4. Validate Feature Datasets...  reports layers whose CRS differs from their dataset.

The grouping is stored in two helper tables inside the .gpkg
(feature_datasets, feature_dataset_members). Other software ignores them.
Close edit mode on the file's layers before creating layers in it.
