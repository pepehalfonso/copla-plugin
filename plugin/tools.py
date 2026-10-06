"""Typed tool registry for Copla.

Every tool is a named, validated operation. There is deliberately no
execute-arbitrary-code tool: safety for a public tool means the AI can
only call operations that were written and reviewed by maintainers.
"""

import os
import sys

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsLayoutExporter,
    QgsMapRendererSequentialJob,
    QgsPalLayerSettings,
    QgsProcessingFeedback,
    QgsProcessingParameterDefinition,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
from qgis.utils import iface

try:
    import processing
except Exception:
    processing = None

PLUGIN_VERSION = "0.1.0"

TOOLS = {}


class ToolError(Exception):
    def __init__(self, message, code="tool_error"):
        super().__init__(message)
        self.code = code


def tool(name, description, params=None, mutates=False):
    def wrap(fn):
        TOOLS[name] = {
            "fn": fn,
            "description": description,
            "params": params or {},
            "mutates": mutates,
        }
        return fn
    return wrap


def validate_args(name, args):
    spec = TOOLS[name]["params"]
    args = dict(args or {})
    for key in args:
        if key not in spec:
            raise ToolError(
                "Unknown argument '%s' for %s. Valid: %s"
                % (key, name, ", ".join(spec) or "(none)"),
                code="bad_args",
            )
    for key, meta in spec.items():
        if meta.get("required") and key not in args:
            raise ToolError("Missing required argument '%s'" % key, code="bad_args")
        if key in args:
            expected = meta.get("type")
            value = args[key]
            ok = True
            if expected == "string":
                ok = isinstance(value, str)
            elif expected == "integer":
                ok = isinstance(value, int) and not isinstance(value, bool)
            elif expected == "number":
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            elif expected == "boolean":
                ok = isinstance(value, bool)
            elif expected == "array":
                ok = isinstance(value, list)
            elif expected == "object":
                ok = isinstance(value, dict)
            if not ok:
                raise ToolError(
                    "Argument '%s' must be of type %s" % (key, expected),
                    code="bad_args",
                )
            if meta.get("enum") and value not in meta["enum"]:
                raise ToolError(
                    "Argument '%s' must be one of: %s"
                    % (key, ", ".join(str(v) for v in meta["enum"])),
                    code="bad_args",
                )
    return args


def run_tool(name, args):
    if name not in TOOLS:
        raise ToolError(
            "Unknown tool '%s'. Use the search/list of available tools." % name,
            code="unknown_tool",
        )
    args = validate_args(name, args)
    result = TOOLS[name]["fn"](**args)
    return result


def _project():
    return QgsProject.instance()


def find_layer(ref):
    if not isinstance(ref, str) or not ref.strip():
        raise ToolError("Layer reference must be a non-empty string", code="bad_args")
    project = _project()
    layer = project.mapLayer(ref)
    if layer is not None:
        return layer
    matches = [l for l in project.mapLayers().values() if l.name() == ref]
    if not matches:
        matches = [l for l in project.mapLayers().values() if ref.lower() in l.name().lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            "Ambiguous layer '%s': %d matches (%s). Use layer id."
            % (ref, len(matches), ", ".join(m.name() for m in matches[:5])),
            code="ambiguous_layer",
        )
    raise ToolError(
        "Layer '%s' not found. Use list_layers to see available layers." % ref,
        code="not_found",
    )


def _layer_type(layer):
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    return "other"


def _is_visible(layer):
    node = _project().layerTreeRoot().findLayer(layer)
    return bool(node.isVisible()) if node is not None else True


def _extent_list(extent):
    if extent is None or extent.isNull():
        return None
    return [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]


def layer_summary(layer):
    info = {
        "id": layer.id(),
        "name": layer.name(),
        "type": _layer_type(layer),
        "crs": layer.crs().authid() or "",
        "visible": _is_visible(layer),
        "extent": _extent_list(layer.extent()),
    }
    if isinstance(layer, QgsVectorLayer):
        info["feature_count"] = layer.featureCount()
        info["geometry"] = QgsWkbTypes.displayString(layer.wkbType())
    elif isinstance(layer, QgsRasterLayer):
        info["width"] = layer.width()
        info["height"] = layer.height()
        info["bands"] = layer.bandCount()
    return info


def _rect_from(value):
    try:
        if isinstance(value, str):
            parts = [float(p) for p in value.replace(",", " ").split()]
        else:
            parts = [float(v) for v in value]
        if len(parts) != 4:
            raise ValueError
        return QgsRectangle(parts[0], parts[1], parts[2], parts[3])
    except Exception:
        raise ToolError(
            "extent must be [xmin, ymin, xmax, ymax] with numbers",
            code="bad_args",
        )


_PALETTE = [
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#984ea3",
    "#ff7f00",
    "#ffff33",
    "#a65628",
    "#f781bf",
    "#66c2a5",
    "#fc8d62",
]


def _field_index(lyr, field):
    idx = lyr.fields().indexFromName(field)
    if idx < 0:
        raise ToolError(
            "Unknown field '%s'. Available: %s"
            % (field, ", ".join(f.name() for f in lyr.fields()) or "(none)"),
            code="bad_args",
        )
    return idx


def _qcolor(value, param="color"):
    color = QColor(value)
    if not color.isValid():
        raise ToolError(
            "%s must be a valid color, e.g. '#ff0000' or 'red'" % param,
            code="bad_args",
        )
    return color


def _make_ramp(name):
    style = QgsStyle.defaultStyle()
    if name:
        ramp = style.colorRamp(name)
        if ramp is None:
            names = sorted(style.colorRampNames())
            raise ToolError(
                "Color ramp '%s' not found. Available: %s"
                % (name, ", ".join(names[:20]) + (", ..." if len(names) > 20 else "")),
                code="bad_args",
            )
        return ramp
    ramp = style.colorRamp("Spectral")
    if ramp is None:
        raise ToolError("Default color ramp 'Spectral' is unavailable", code="tool_error")
    return ramp


def _vector_layer(layer, tool_name="This tool"):
    lyr = find_layer(layer)
    if not isinstance(lyr, QgsVectorLayer):
        raise ToolError(
            "%s requires a vector layer; '%s' is not one" % (tool_name, lyr.name()),
            code="bad_args",
        )
    return lyr


class _CollectFeedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []

    def _add(self, text):
        if len(self.lines) < 500:
            self.lines.append(str(text))

    def pushInfo(self, info):
        self._add(info)

    def pushWarning(self, warning):
        self._add("WARN: " + warning)

    def pushCommandInfo(self, info):
        self._add(info)

    def pushConsoleInfo(self, info):
        self._add(info)

    def reportError(self, error, fatalError=False):
        self._add("ERROR: " + error)

    def setProgressText(self, text):
        self._add(text)


# ---------------------------------------------------------------- diagnostics

@tool("diagnose", "Health check: returns QGIS/plugin versions, project state and whether Processing is available. Call this first if something seems broken.")
def diagnose():
    return {
        "ok": True,
        "plugin_version": PLUGIN_VERSION,
        "qgis_version": Qgis.QGIS_VERSION,
        "python": sys.version.split()[0],
        "processing_available": processing is not None,
        "project_file": _project().fileName() or None,
        "layer_count": len(_project().mapLayers()),
        "platform": sys.platform,
    }


# ---------------------------------------------------------------- project

@tool("get_project_info", "Current project info: file path, title, CRS, extent and layer count.")
def get_project_info():
    project = _project()
    canvas = iface.mapCanvas() if iface is not None else None
    return {
        "file": project.fileName() or None,
        "title": project.title() or None,
        "crs": project.crs().authid() or None,
        "extent": _extent_list(canvas.extent()) if canvas is not None else None,
        "layer_count": len(project.mapLayers()),
        "layer_groups": [g.name() for g in project.layerTreeRoot().findGroups()],
    }


@tool("load_project", "Open a QGIS project file (.qgz or .qgs), replacing the current project.", params={
    "path": {"type": "string", "required": True, "description": "Absolute path to the .qgz/.qgs file"},
}, mutates=True)
def load_project(path):
    if not os.path.isfile(path):
        raise ToolError("File not found: %s" % path, code="not_found")
    ok = _project().read(path)
    if not ok:
        raise ToolError("QGIS could not read the project: %s" % path, code="io_error")
    return get_project_info()


@tool("save_project", "Save the project. Provide path to save as a new file; omit it to overwrite the current file.", params={
    "path": {"type": "string", "required": False, "description": "Destination .qgz path; omit to save to the current file"},
}, mutates=True)
def save_project(path=None):
    project = _project()
    target = path or project.fileName()
    if not target:
        raise ToolError("Project has no file yet; provide 'path' to save it.", code="bad_args")
    ok = project.write(target)
    if not ok:
        raise ToolError("Could not write project to %s" % target, code="io_error")
    return {"saved": os.path.abspath(target)}


@tool("set_project_crs", "Set the project CRS, e.g. 'EPSG:4326'. Layers reproject on the fly.", params={
    "crs": {"type": "string", "required": True, "description": "CRS identifier, e.g. EPSG:4326 or EPSG:32721"},
}, mutates=True)
def set_project_crs(crs):
    target = QgsCoordinateReferenceSystem(crs)
    if not target.isValid():
        raise ToolError(
            "Invalid CRS: %s (use an authid like EPSG:4326)" % crs,
            code="bad_args",
        )
    _project().setCrs(target)
    if iface is not None:
        iface.mapCanvas().refresh()
    return {"crs": target.authid(), "name": target.description()}


# ---------------------------------------------------------------- layers

@tool("list_layers", "List all layers in the project with id, name, type, CRS, visibility and extent. Use the returned 'id' as the 'layer' argument of other tools.")
def list_layers():
    return [layer_summary(l) for l in _project().mapLayers().values()]


@tool("get_layer_info", "Detailed info for one layer: fields, geometry type, CRS, extent, renderer and provider. 'layer' accepts a layer id or name.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id (preferred) or layer name"},
})
def get_layer_info(layer):
    lyr = find_layer(layer)
    info = layer_summary(lyr)
    info["provider"] = lyr.providerType()
    info["renderer"] = lyr.renderer().type() if lyr.renderer() else None
    info["editable"] = lyr.isEditable()
    if isinstance(lyr, QgsVectorLayer):
        info["fields"] = [
            {"name": f.name(), "type": f.typeName()} for f in lyr.fields()
        ]
        info["geometry"] = QgsWkbTypes.displayString(lyr.wkbType())
    return info


@tool("add_layer", "Add a vector or raster file as a layer. Vector via OGR (shp, geojson, gpkg, ...) and raster via GDAL (tif, img, ...). Provider is chosen from the file.", params={
    "path": {"type": "string", "required": True, "description": "Absolute path to the data file (or OGR/GDAL connection string)"},
    "name": {"type": "string", "required": False, "description": "Layer name; defaults to the file name"},
    "group": {"type": "string", "required": False, "description": "Place the layer inside this layer-tree group (created if missing)"},
}, mutates=True)
def add_layer(path, name=None, group=None):
    if not os.path.exists(path) and "://" not in path:
        raise ToolError("File not found: %s" % path, code="not_found")
    base = name or os.path.splitext(os.path.basename(path))[0]
    vector = QgsVectorLayer(path, base, "ogr")
    if not vector.isValid():
        vector = None
        raster = QgsRasterLayer(path, base)
        if not raster.isValid():
            raise ToolError(
                "Could not load '%s' as vector (OGR) or raster (GDAL)." % path,
                code="io_error",
            )
        layer = raster
    else:
        layer = vector
    project = _project()
    if group:
        root = project.layerTreeRoot()
        node = root.findGroup(group)
        if node is None:
            node = root.addGroup(group)
        node.addLayer(layer)
    else:
        project.addMapLayer(layer)
    return layer_summary(layer)


@tool("remove_layer", "Remove a layer from the project (does not delete the file on disk).", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
}, mutates=True)
def remove_layer(layer):
    lyr = find_layer(layer)
    name = lyr.name()
    _project().removeMapLayer(lyr.id())
    return {"removed": name}


@tool("rename_layer", "Rename a layer.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "name": {"type": "string", "required": True, "description": "New layer name"},
}, mutates=True)
def rename_layer(layer, name):
    lyr = find_layer(layer)
    old = lyr.name()
    lyr.setName(name)
    return {"renamed": old, "to": name}


@tool("set_layer_visibility", "Show or hide a layer in the map canvas and layer tree.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "visible": {"type": "boolean", "required": True, "description": "true to show, false to hide"},
}, mutates=True)
def set_layer_visibility(layer, visible):
    lyr = find_layer(layer)
    node = _project().layerTreeRoot().findLayer(lyr)
    if node is None:
        raise ToolError("Layer has no tree node", code="tool_error")
    node.setItemVisibilityChecked(visible)
    if iface is not None:
        iface.mapCanvas().refresh()
    return {"layer": lyr.name(), "visible": bool(visible)}


@tool("zoom_to_layer", "Pan and zoom the map canvas to a layer's extent.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
}, mutates=True)
def zoom_to_layer(layer):
    lyr = find_layer(layer)
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    iface.setActiveLayer(lyr)
    iface.mapCanvas().zoomToLayer(lyr)
    iface.mapCanvas().refresh()
    return {"zoomed_to": lyr.name()}


@tool("add_basemap", "Add an XYZ basemap (OpenStreetMap or satellite imagery) or a custom XYZ tile URL, placed at the bottom of the layer list.", params={
    "style": {"type": "string", "required": False, "enum": ["osm", "satellite"], "description": "Basemap style (default osm); ignored if url is given"},
    "url": {"type": "string", "required": False, "description": "Custom XYZ tile URL with {z}/{x}/{y} placeholders"},
    "name": {"type": "string", "required": False, "description": "Display name for the basemap layer"},
}, mutates=True)
def add_basemap(style="osm", url=None, name=None):
    templates = {
        "osm": ("OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        "satellite": ("Esri Satellite", "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
    }
    if url:
        for token in ("{z}", "{x}", "{y}"):
            if token not in url:
                raise ToolError("Custom url must contain %s placeholders" % token, code="bad_args")
        tile_url = url
        label = name or "XYZ basemap"
    else:
        if style not in templates:
            raise ToolError("style must be one of: %s" % ", ".join(templates), code="bad_args")
        label, tile_url = templates[style]
        if name:
            label = name
    layer = QgsRasterLayer("type=xyz&url=%s" % tile_url, label, "wms")
    if not layer.isValid():
        raise ToolError(
            "Could not load the basemap layer (check network access and url)",
            code="io_error",
        )
    project = _project()
    project.addMapLayer(layer, False)
    project.layerTreeRoot().addLayer(layer)
    if iface is not None:
        iface.mapCanvas().refresh()
    return layer_summary(layer)


# ---------------------------------------------------------------- features

@tool("get_features", "Read features from a vector layer as JSON records. Supports an optional subset filter (QGIS expression) and field selection.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "limit": {"type": "integer", "required": False, "description": "Max features to return (default 50, max 1000)"},
    "filter": {"type": "string", "required": False, "description": "QGIS expression to filter features, e.g. pop > 1000"},
    "fields": {"type": "array", "required": False, "description": "Only return these fields (default: all)"},
    "include_geometry": {"type": "boolean", "required": False, "description": "Include geometry as WKT (default false; can be large)"},
})
def get_features(layer, limit=50, filter=None, fields=None, include_geometry=False):
    lyr = find_layer(layer)
    if not isinstance(lyr, QgsVectorLayer):
        raise ToolError("Layer '%s' is not a vector layer" % lyr.name(), code="bad_args")
    limit = max(1, min(int(limit), 1000))
    request = QgsFeatureRequest().setLimit(limit)
    if filter:
        probe = QgsExpression(filter)
        if probe.hasParserError():
            raise ToolError("Invalid filter expression: %s" % probe.parserErrorString(), code="bad_args")
        request.setFilterExpression(filter)
    wanted = None
    if fields:
        available = {f.name() for f in lyr.fields()}
        missing = [f for f in fields if f not in available]
        if missing:
            raise ToolError(
                "Unknown field(s): %s. Available: %s"
                % (", ".join(missing), ", ".join(sorted(available))),
                code="bad_args",
            )
        wanted = list(fields)
        request.setSubsetOfAttributes([lyr.fields().indexFromName(n) for n in wanted])
    out = []
    total = 0
    for feat in lyr.getFeatures(request):
        total += 1
        rec = {}
        for name in (wanted or [f.name() for f in lyr.fields()]):
            value = feat[name]
            rec[name] = None if value is None else (str(value) if not isinstance(value, (int, float, bool)) else value)
        if include_geometry and feat.hasGeometry():
            rec["wkt"] = feat.geometry().asWkt()
        out.append(rec)
    return {
        "layer": lyr.name(),
        "total_returned": total,
        "has_more": total == limit,
        "features": out,
    }


@tool("select_features", "Select vector features matching a QGIS expression (selection persists in the QGIS UI).", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "expression": {"type": "string", "required": True, "description": "QGIS expression, e.g. pop > 10000 AND type = 'city'"},
}, mutates=True)
def select_features(layer, expression):
    lyr = find_layer(layer)
    if not isinstance(lyr, QgsVectorLayer):
        raise ToolError("Layer '%s' is not a vector layer" % lyr.name(), code="bad_args")
    lyr.selectByExpression(expression)
    return {"layer": lyr.name(), "selected": lyr.selectedFeatureCount()}


@tool("run_expression", "Evaluate a QGIS expression. Provide either 'feature_id' to evaluate on one feature, or 'aggregate' to summarize a whole layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "expression": {"type": "string", "required": True, "description": "Expression to evaluate, e.g. $area or 2 * population"},
    "feature_id": {"type": "integer", "required": False, "description": "Evaluate for this feature id (FID)"},
    "aggregate": {"type": "object", "required": False, "description": "Aggregate mode, e.g. {function: sum, expression: area}. function: sum, min, max, mean, count, unique"},
})
def run_expression(layer, expression, feature_id=None, aggregate=None):
    lyr = find_layer(layer)
    if not isinstance(lyr, QgsVectorLayer):
        raise ToolError("Layer '%s' is not a vector layer" % lyr.name(), code="bad_args")
    if aggregate and feature_id is not None:
        raise ToolError("Pass either feature_id or aggregate, not both", code="bad_args")
    if aggregate:
        fn = aggregate.get("function")
        exp = aggregate.get("expression", expression)
        if fn not in ("sum", "min", "max", "mean", "count", "unique"):
            raise ToolError(
                "aggregate.function must be one of: sum, min, max, mean, count, unique",
                code="bad_args",
            )
        expr = QgsExpression(exp)
        if expr.hasParserError():
            raise ToolError("Expression parse error: %s" % expr.parserErrorString(), code="bad_args")
        base_context = lyr.createExpressionContext()
        values = []
        for feat in lyr.getFeatures():
            context = QgsExpressionContext(base_context)
            context.setFeature(feat)
            value = expr.evaluate(context)
            if expr.hasEvalError():
                raise ToolError("Expression eval error: %s" % expr.evalErrorString(), code="tool_error")
            if value is not None and not (isinstance(value, float) and value != value):
                values.append(value)
        if fn == "count":
            result = len(values)
        elif fn == "unique":
            result = len({str(v) for v in values})
        elif fn in ("sum", "min", "max", "mean"):
            if not values:
                raise ToolError("No values to aggregate (empty layer or all NULL)", code="tool_error")
            numbers = [float(v) for v in values]
            if fn == "sum":
                result = sum(numbers)
            elif fn == "min":
                result = min(numbers)
            elif fn == "max":
                result = max(numbers)
            else:
                result = sum(numbers) / len(numbers)
            if isinstance(result, float) and result.is_integer():
                result = int(result)
        return {"aggregate": fn, "value": result if isinstance(result, (int, float, str, bool)) else str(result)}
    if feature_id is None:
        raise ToolError("Provide feature_id or aggregate", code="bad_args")
    request = QgsFeatureRequest().setFilterFid(int(feature_id))
    feat = next(iter(lyr.getFeatures(request)), None)
    if feat is None:
        raise ToolError("Feature with id %s not found" % feature_id, code="not_found")
    expr = QgsExpression(expression)
    context = lyr.createExpressionContext()
    context.setFeature(feat)
    if expr.hasParserError():
        raise ToolError("Expression parse error: %s" % expr.parserErrorString(), code="bad_args")
    value = expr.evaluate(context)
    if expr.hasEvalError():
        raise ToolError("Expression eval error: %s" % expr.evalErrorString(), code="tool_error")
    return {"feature_id": feature_id, "value": value if isinstance(value, (int, float, str, bool)) else str(value)}


def _resolve_fids(lyr, expression, feature_ids):
    if expression and feature_ids:
        raise ToolError("Provide expression or feature_ids, not both", code="bad_args")
    if not expression and not feature_ids:
        raise ToolError("Provide expression or feature_ids", code="bad_args")
    if expression:
        probe = QgsExpression(expression)
        if probe.hasParserError():
            raise ToolError("Expression parse error: %s" % probe.parserErrorString(), code="bad_args")
        request = QgsFeatureRequest().setFilterExpression(expression)
        fids = [f.id() for f in lyr.getFeatures(request)]
    else:
        fids = [int(v) for v in feature_ids]
    if len(fids) > 5000:
        raise ToolError(
            "Too many features match (%d, limit 5000); refine the expression" % len(fids),
            code="bad_args",
        )
    return fids


@tool("add_features", "Add features to a vector layer. Each item: {\"geometry\": \"WKT\", \"attributes\": {\"field\": value}}. Geometry is optional for table layers. Edits are committed when done.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name (must be editable)"},
    "features": {"type": "array", "required": True, "description": "List of {geometry (WKT), attributes (object)}; max 500 per call"},
}, mutates=True)
def add_features(layer, features):
    lyr = _vector_layer(layer, "add_features")
    if len(features) > 500:
        raise ToolError("Too many features in one call (limit 500)", code="bad_args")
    if not lyr.startEditing():
        raise ToolError(
            "Layer '%s' is not editable (read-only provider?)" % lyr.name(),
            code="tool_error",
        )
    fields = lyr.fields()
    added = 0
    try:
        for item in features:
            if not isinstance(item, dict):
                raise ToolError("Each feature must be an object with geometry/attributes", code="bad_args")
            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                raise ToolError("'attributes' must be an object", code="bad_args")
            feat = QgsFeature(fields)
            for key, value in attrs.items():
                feat.setAttribute(_field_index(lyr, key), value)
            wkt = item.get("geometry")
            if wkt:
                geom = QgsGeometry.fromWkt(str(wkt))
                if geom is None or geom.isEmpty():
                    raise ToolError("Invalid geometry (not valid WKT): %s" % str(wkt)[:80], code="bad_args")
                if geom.type() != lyr.geometryType():
                    raise ToolError(
                        "Geometry type mismatch: layer is %s, got %s"
                        % (QgsWkbTypes.geometryDisplayString(lyr.geometryType()),
                           QgsWkbTypes.geometryDisplayString(geom.type())),
                        code="bad_args",
                    )
                if QgsWkbTypes.isMultiType(lyr.wkbType()) and not geom.isMultipart():
                    geom.convertToMultiType()
                elif not QgsWkbTypes.isMultiType(lyr.wkbType()) and geom.isMultipart():
                    if not geom.convertToSingleType():
                        raise ToolError("Multi-part geometry does not fit a single-part layer", code="bad_args")
                feat.setGeometry(geom)
            if not lyr.addFeature(feat):
                raise ToolError("QGIS rejected a feature (check attributes/geometry)", code="tool_error")
            added += 1
        if not lyr.commitChanges():
            raise ToolError("Could not commit the new features", code="tool_error")
    except Exception:
        lyr.rollBack()
        raise
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "added": added}


@tool("delete_features", "Delete features from a vector layer, selected by QGIS expression or by feature ids. Edits are committed when done.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name (must be editable)"},
    "expression": {"type": "string", "required": False, "description": "QGIS expression; all matching features are deleted, e.g. pop = 0"},
    "feature_ids": {"type": "array", "required": False, "description": "Feature ids to delete (alternative to expression)"},
}, mutates=True)
def delete_features(layer, expression=None, feature_ids=None):
    lyr = _vector_layer(layer, "delete_features")
    fids = _resolve_fids(lyr, expression, feature_ids)
    if not fids:
        return {"layer": lyr.name(), "deleted": 0, "note": "No features matched"}
    if not lyr.startEditing():
        raise ToolError("Layer '%s' is not editable" % lyr.name(), code="tool_error")
    try:
        if not lyr.deleteFeatures(fids):
            raise ToolError("QGIS could not delete the features", code="tool_error")
        if not lyr.commitChanges():
            raise ToolError("Could not commit the deletion", code="tool_error")
    except Exception:
        lyr.rollBack()
        raise
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "deleted": len(fids)}


@tool("update_attributes", "Update attribute values on existing features (by expression or feature ids). Example values: {\"pop\": 1200, \"name\": \"new\"}.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name (must be editable)"},
    "values": {"type": "object", "required": True, "description": "Object of field name → new value"},
    "expression": {"type": "string", "required": False, "description": "QGIS expression selecting the features to update"},
    "feature_ids": {"type": "array", "required": False, "description": "Feature ids to update (alternative to expression)"},
}, mutates=True)
def update_attributes(layer, values, expression=None, feature_ids=None):
    lyr = _vector_layer(layer, "update_attributes")
    if not values:
        raise ToolError("'values' must not be empty", code="bad_args")
    fids = _resolve_fids(lyr, expression, feature_ids)
    if not fids:
        return {"layer": lyr.name(), "updated": 0, "note": "No features matched"}
    indexes = {key: _field_index(lyr, key) for key in values}
    if not lyr.startEditing():
        raise ToolError("Layer '%s' is not editable" % lyr.name(), code="tool_error")
    updated = 0
    try:
        for fid in fids:
            feat = lyr.getFeature(fid)
            if not feat.isValid():
                raise ToolError("Feature with id %s not found" % fid, code="not_found")
            for key, value in values.items():
                feat.setAttribute(indexes[key], value)
            if not lyr.updateFeature(feat):
                raise ToolError("QGIS rejected the update on feature %s" % fid, code="tool_error")
            updated += 1
        if not lyr.commitChanges():
            raise ToolError("Could not commit the changes", code="tool_error")
    except Exception:
        lyr.rollBack()
        raise
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "updated": updated, "fields": list(values)}


# ---------------------------------------------------------------- style

@tool("set_renderer", "Set vector layer symbology: 'single' (one color), 'categorized' (unique values of a field) or 'graduated' (numeric classes). Optionally pass custom colors or a color ramp name.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "type": {"type": "string", "required": True, "enum": ["single", "categorized", "graduated"], "description": "Renderer type"},
    "field": {"type": "string", "required": False, "description": "Field for categorized/graduated (required for those types)"},
    "color": {"type": "string", "required": False, "description": "Fill color for type=single, e.g. '#4daf4a' (default green)"},
    "colors": {"type": "array", "required": False, "description": "Custom color list for categories (cycles if shorter than classes)"},
    "ramp": {"type": "string", "required": False, "description": "Color ramp name for graduated, e.g. 'Spectral', 'Blues', 'Reds', 'YlOrRd' (default Spectral)"},
    "classes": {"type": "integer", "required": False, "description": "Number of classes for graduated (default 5, max 20)"},
}, mutates=True)
def set_renderer(layer, type, field=None, color=None, colors=None, ramp=None, classes=None):
    lyr = _vector_layer(layer, "set_renderer")
    gtype = lyr.geometryType()
    if type in ("categorized", "graduated") and not field:
        raise ToolError("field is required for type='%s'" % type, code="bad_args")
    if type == "single":
        symbol = QgsSymbol.defaultSymbol(gtype)
        symbol.setColor(_qcolor(color) if color else QColor("#4daf4a"))
        renderer = QgsSingleSymbolRenderer(symbol)
    elif type == "categorized":
        idx = _field_index(lyr, field)
        values = lyr.uniqueValues(idx)
        if len(values) > 40:
            raise ToolError(
                "%d unique values in '%s' (limit 40); use type='graduated'"
                % (len(values), field),
                code="bad_args",
            )
        palette = [_qcolor(c, "colors") for c in (colors or _PALETTE)]
        categories = []
        for i, value in enumerate(sorted(values, key=lambda v: (v is None, str(v)))):
            symbol = QgsSymbol.defaultSymbol(gtype)
            symbol.setColor(palette[i % len(palette)])
            label = "(empty)" if value is None or value == "" else str(value)
            categories.append(QgsRendererCategory(value, symbol, label))
        renderer = QgsCategorizedSymbolRenderer(field, categories)
    else:
        idx = _field_index(lyr, field)
        if not lyr.fields()[idx].isNumeric():
            raise ToolError("Field '%s' is not numeric; use type='categorized'" % field, code="bad_args")
        mn = lyr.minimumValue(idx)
        mx = lyr.maximumValue(idx)
        if mn is None or mx is None:
            raise ToolError("Field '%s' has no values" % field, code="tool_error")
        mn, mx = float(mn), float(mx)
        n = max(2, min(int(classes or 5), 20))
        ramp_obj = _make_ramp(ramp)
        ranges = []
        if mn == mx:
            symbol = QgsSymbol.defaultSymbol(gtype)
            symbol.setColor(ramp_obj.color(0.5))
            ranges.append(QgsRendererRange(mn, mx, symbol, str(mn)))
        else:
            step = (mx - mn) / n
            for i in range(n):
                lo = mn + i * step
                hi = mx if i == n - 1 else mn + (i + 1) * step
                symbol = QgsSymbol.defaultSymbol(gtype)
                symbol.setColor(ramp_obj.color((i + 0.5) / n))
                ranges.append(QgsRendererRange(lo, hi, symbol, "%.6g - %.6g" % (lo, hi)))
        renderer = QgsGraduatedSymbolRenderer(field, ranges)
    lyr.setRenderer(renderer)
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "renderer": type, "field": field}


@tool("set_labels", "Configure labels on a vector layer (enable with a field, size, color and optional white halo, or pass enabled=false to turn labels off).", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "enabled": {"type": "boolean", "required": True, "description": "true to show labels, false to hide them"},
    "field": {"type": "string", "required": False, "description": "Field name, or expression if expression=true (required when enabling)"},
    "size": {"type": "number", "required": False, "description": "Label size in points (default 10)"},
    "color": {"type": "string", "required": False, "description": "Text color (default '#000000')"},
    "halo": {"type": "boolean", "required": False, "description": "White halo behind text (default true)"},
    "expression": {"type": "boolean", "required": False, "description": "Treat 'field' as a QGIS expression (default false)"},
}, mutates=True)
def set_labels(layer, enabled, field=None, size=10, color="#000000", halo=True, expression=False):
    lyr = _vector_layer(layer, "set_labels")
    if not enabled:
        lyr.setLabelsEnabled(False)
        if iface is not None:
            lyr.triggerRepaint()
        return {"layer": lyr.name(), "labels": False}
    if not field:
        raise ToolError("field is required when enabled=true", code="bad_args")
    if not expression:
        _field_index(lyr, field)
    settings = QgsPalLayerSettings()
    settings.fieldName = field
    settings.isExpression = bool(expression)
    text = QgsTextFormat()
    text.setSize(float(size))
    text.setColor(_qcolor(color))
    if halo:
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(True)
        buffer.setSize(1.5)
        buffer.setColor(QColor("#ffffff"))
        text.setBuffer(buffer)
    settings.setFormat(text)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    lyr.setLabelsEnabled(True)
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "labels": True, "field": field, "size": float(size)}


# ---------------------------------------------------------------- view & selection

@tool("set_extent", "Zoom the map canvas to an extent [xmin, ymin, xmax, ymax]. Optionally pass the CRS of those coordinates if it differs from the project CRS.", params={
    "extent": {"type": "array", "required": True, "description": "[xmin, ymin, xmax, ymax]"},
    "crs": {"type": "string", "required": False, "description": "CRS of the extent coordinates, e.g. EPSG:4326 (default: project CRS)"},
}, mutates=True)
def set_extent(extent, crs=None):
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    rect = _rect_from(extent)
    if crs:
        source = QgsCoordinateReferenceSystem(crs)
        if not source.isValid():
            raise ToolError("Invalid CRS: %s" % crs, code="bad_args")
        target = _project().crs()
        if source != target:
            rect = QgsCoordinateTransform(source, target, _project()).transformBoundingBox(rect)
    canvas = iface.mapCanvas()
    canvas.setExtent(rect)
    canvas.refresh()
    return {"extent": _extent_list(canvas.extent())}


@tool("clear_selection", "Clear the feature selection on a layer, or on all layers if 'layer' is omitted.", params={
    "layer": {"type": "string", "required": False, "description": "Layer id or name (omit to clear all layers)"},
}, mutates=True)
def clear_selection(layer=None):
    project = _project()
    cleared = []
    if layer:
        lyr = _vector_layer(layer, "clear_selection")
        lyr.removeSelection()
        cleared.append(lyr.name())
    else:
        for candidate in project.mapLayers().values():
            if isinstance(candidate, QgsVectorLayer) and candidate.selectedFeatureCount() > 0:
                candidate.removeSelection()
                cleared.append(candidate.name())
    if iface is not None:
        iface.mapCanvas().refresh()
    return {"cleared": cleared}


@tool("zoom_to_selection", "Zoom the map canvas to the selected features of a layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name with a selection"},
}, mutates=True)
def zoom_to_selection(layer):
    lyr = _vector_layer(layer, "zoom_to_selection")
    count = lyr.selectedFeatureCount()
    if count == 0:
        raise ToolError(
            "Layer '%s' has no selected features (use select_features first)" % lyr.name(),
            code="tool_error",
        )
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    iface.mapCanvas().zoomToSelected(lyr)
    iface.mapCanvas().refresh()
    return {"layer": lyr.name(), "selected": count, "extent": _extent_list(iface.mapCanvas().extent())}


# ---------------------------------------------------------------- processing

@tool("search_algorithms", "Search the Processing toolbox for algorithms by keyword (matches id, name and group). Returns algorithm ids usable with run_algorithm.", params={
    "query": {"type": "string", "required": False, "description": "Keywords, e.g. 'buffer' or 'zonal statistics'"},
    "limit": {"type": "integer", "required": False, "description": "Max results (default 15, max 50)"},
})
def search_algorithms(query="", limit=15):
    if processing is None:
        raise ToolError("Processing plugin is not available", code="tool_error")
    limit = max(1, min(int(limit), 50))
    q = (query or "").lower()
    results = []
    for alg in QgsApplication.processingRegistry().algorithms():
        hay = "%s %s %s %s" % (alg.id(), alg.displayName(), alg.name(), alg.group())
        if not q or q in hay.lower():
            results.append({
                "id": alg.id(),
                "name": alg.displayName(),
                "group": alg.group(),
            })
        if len(results) >= limit:
            break
    return {"count": len(results), "algorithms": results}


@tool("get_algorithm_info", "Describe a Processing algorithm: parameters (names, types, defaults, enum options), outputs and help text. Use it before run_algorithm to fill params correctly.", params={
    "id": {"type": "string", "required": True, "description": "Algorithm id, e.g. native:buffer (find ids with search_algorithms)"},
})
def get_algorithm_info(id):
    if processing is None:
        raise ToolError("Processing plugin is not available", code="tool_error")
    alg = QgsApplication.processingRegistry().algorithmById(id)
    if alg is None:
        raise ToolError("Unknown algorithm '%s' (use search_algorithms)" % id, code="not_found")
    parameters = []
    for p in alg.parameterDefinitions():
        entry = {
            "name": p.name(),
            "description": p.description() or "",
            "type": p.type(),
            "default": None,
            "optional": False,
            "options": None,
        }
        try:
            default = p.defaultValue()
            entry["default"] = (
                default if isinstance(default, (str, int, float, bool)) or default is None
                else str(default)
            )
        except Exception:
            pass
        try:
            entry["optional"] = bool(p.flags() & QgsProcessingParameterDefinition.FlagOptional)
        except AttributeError:
            try:
                entry["optional"] = bool(p.flags() & Qgis.ProcessingParameterFlag.Optional)
            except Exception:
                pass
        try:
            options = p.options()
            if options and isinstance(options[0], str):
                entry["options"] = [str(o) for o in options]
        except Exception:
            pass
        if entry["options"] is None:
            try:
                choices = p.choices()
                if choices and isinstance(choices[0], str):
                    entry["options"] = [str(c) for c in choices]
            except Exception:
                pass
        parameters.append(entry)
    outputs = [
        {"name": o.name(), "description": o.description() or ""}
        for o in alg.outputDefinitions()
    ]
    try:
        help_text = (alg.shortHelpString() or "")[:600]
    except Exception:
        help_text = ""
    return {
        "id": alg.id(),
        "name": alg.displayName(),
        "group": alg.group(),
        "help": help_text,
        "parameters": parameters,
        "outputs": outputs,
    }


@tool("run_algorithm", "Run a Processing algorithm synchronously and return its outputs. Find ids with search_algorithms. Layer arguments accept a layer id or name. Can take minutes for heavy algorithms - QGIS stays responsive between requests but this call blocks until finished.", params={
    "id": {"type": "string", "required": True, "description": "Algorithm id, e.g. native:buffer"},
    "params": {"type": "object", "required": True, "description": "Algorithm parameters as a JSON object; layer values may be layer ids or names; outputs are file paths"},
}, mutates=True)
def run_algorithm(id, params):
    if processing is None:
        raise ToolError("Processing plugin is not available", code="tool_error")
    alg = QgsApplication.processingRegistry().algorithmById(id)
    if alg is None:
        raise ToolError("Algorithm not found: %s (use search_algorithms)" % id, code="not_found")
    resolved = {}
    layer_names = {l.name(): l for l in _project().mapLayers().values()}
    for key, value in params.items():
        definition = alg.parameterDefinition(key)
        if definition is not None and definition.isDestination():
            resolved[key] = value
        elif isinstance(value, str) and value in layer_names:
            resolved[key] = layer_names[value]
        else:
            resolved[key] = value
    feedback = _CollectFeedback()
    try:
        result = processing.run(id, resolved, feedback=feedback)
    except Exception as exc:
        raise ToolError(
            "Algorithm failed: %s\n%s" % (exc, "\n".join(feedback.lines[-20:])),
            code="run_error",
        )
    outputs = {}
    for key, value in (result or {}).items():
        definition = alg.parameterDefinition(key)
        if definition is not None and definition.isDestination():
            outputs[key] = str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            outputs[key] = value
        else:
            try:
                outputs[key] = str(value)
            except Exception:
                outputs[key] = repr(value)
    return {"algorithm": id, "outputs": outputs, "log": feedback.lines[-50:]}


# ---------------------------------------------------------------- rendering

@tool("render_map", "Render the current map view to an image file (png/jpg). Optionally set size, extent and/or output CRS without changing the user's view permanently.", params={
    "output_path": {"type": "string", "required": True, "description": "Destination file, e.g. C:/maps/out.png"},
    "width": {"type": "integer", "required": False, "description": "Image width in px (default 1600)"},
    "height": {"type": "integer", "required": False, "description": "Image height in px (default 1000)"},
    "extent": {"type": "array", "required": False, "description": "[xmin, ymin, xmax, ymax] to render instead of the current view"},
    "crs": {"type": "string", "required": False, "description": "Output CRS, e.g. EPSG:4326 (default: current project CRS)"},
}, mutates=False)
def render_map(output_path, width=1600, height=1000, extent=None, crs=None):
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    canvas = iface.mapCanvas()
    settings = canvas.mapSettings()
    settings.setOutputSize(QSize(int(width), int(height)))
    if extent is not None:
        settings.setExtent(_rect_from(extent))
    if crs:
        out_crs = QgsCoordinateReferenceSystem(crs)
        if not out_crs.isValid():
            raise ToolError("Invalid CRS: %s" % crs, code="bad_args")
        settings.setOutputCrs(out_crs)
    job = QgsMapRendererSequentialJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    if image.isNull():
        raise ToolError("Render produced an empty image", code="tool_error")
    folder = os.path.dirname(os.path.abspath(output_path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    fmt = os.path.splitext(output_path)[1].lstrip(".").upper() or "PNG"
    if fmt == "JPG":
        fmt = "JPEG"
    if not image.save(output_path, fmt):
        raise ToolError("Could not save image to %s" % output_path, code="io_error")
    return {"saved": os.path.abspath(output_path), "width": image.width(), "height": image.height()}


# ---------------------------------------------------------------- layouts

@tool("list_layouts", "List print layouts (atlas/composer layouts) in the project.")
def list_layouts():
    out = []
    for layout in _project().printLayouts():
        pages = []
        for i in range(layout.pageCollection().pageCount()):
            page = layout.pageCollection().page(i)
            pages.append({"width_mm": page.pageSize().width(), "height_mm": page.pageSize().height()})
        out.append({"name": layout.name(), "pages": pages})
    return {"count": len(out), "layouts": out}


@tool("export_layout", "Export a print layout to PDF or an image (png/jpg) using the layout's own page size. Use list_layouts for names.", params={
    "name": {"type": "string", "required": True, "description": "Layout name"},
    "output_path": {"type": "string", "required": True, "description": "Destination file (.pdf, .png or .jpg)"},
    "dpi": {"type": "integer", "required": False, "description": "Resolution for image export (default 300)"},
}, mutates=False)
def export_layout(name, output_path, dpi=300):
    layout = None
    for candidate in _project().printLayouts():
        if candidate.name() == name:
            layout = candidate
            break
    if layout is None:
        raise ToolError(
            "Layout '%s' not found. Available: %s"
            % (name, ", ".join(l.name() for l in _project().printLayouts()) or "(none)"),
            code="not_found",
        )
    folder = os.path.dirname(os.path.abspath(output_path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    exporter = QgsLayoutExporter(layout)
    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".pdf":
        settings = QgsLayoutExporter.PdfExportSettings()
        status = exporter.exportToPdf(output_path, settings)
    else:
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = int(dpi)
        status = exporter.exportToImage(output_path, settings)
    if status != QgsLayoutExporter.Success:
        raise ToolError("Export failed with status %s" % status, code="io_error")
    return {"saved": os.path.abspath(output_path)}


def tools_manifest():
    return [
        {
            "name": name,
            "description": spec["description"],
            "params": spec["params"],
            "mutates": spec["mutates"],
        }
        for name, spec in TOOLS.items()
    ]
