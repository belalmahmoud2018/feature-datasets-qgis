# Feature Datasets for GeoPackage
# Copyright (C) 2026 Belal Mahmoud Abdelmonem
# Licensed under the GNU General Public License v2 or later (see LICENSE).

def classFactory(iface):
    from .plugin import FeatureDatasetsPlugin
    return FeatureDatasetsPlugin(iface)
