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
    QgsCoordinateReferenceSystem,
    QgsExpression,
    QgsExpressionContext,
    QgsFeatureRequest,
    QgsLayoutExporter,
    QgsMapRendererSequentialJob,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QSize
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
        request.setSubsetOfNames(wanted)
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
