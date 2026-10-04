def classFactory(iface):
    from .plugin import FeatureDatasetsPlugin
    return FeatureDatasetsPlugin(iface)
