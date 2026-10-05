def classFactory(iface):
    from .copla_plugin import CoplaPlugin
    return CoplaPlugin(iface)
