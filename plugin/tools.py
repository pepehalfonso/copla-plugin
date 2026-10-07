"""Typed tool registry for Copla.

Every tool is a named, validated operation. There is deliberately no
execute-arbitrary-code tool: safety for a public tool means the AI can
only call operations that were written and reviewed by maintainers.
"""

import json as _json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

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
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsLayoutExporter,
    QgsLayoutItemMap,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapRendererSequentialJob,
    QgsNetworkAccessManager,
    QgsPalLayerSettings,
    QgsPrintLayout,
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
    QgsUnitTypes,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QByteArray, QEventLoop, QSize, QTimer, QUrl, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
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


def _load_layer_from_path(path, name=None, group=None):
    base = name or os.path.splitext(os.path.basename(path.rstrip("/")))[0]
    load_path = path
    if path.lower().endswith(".zip") and not path.lower().startswith("/vsizip/"):
        load_path = "/vsizip/" + path.replace("\\", "/")
    vector = QgsVectorLayer(load_path, base, "ogr")
    if not vector.isValid():
        vector = None
        raster = QgsRasterLayer(load_path, base)
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
        project.addMapLayer(layer, False)
        node.addLayer(layer)
    else:
        project.addMapLayer(layer)
    return layer_summary(layer)


@tool("add_layer", "Add a vector or raster file as a layer. Vector via OGR (shp, geojson, gpkg, ...) and raster via GDAL (tif, img, ...). Provider is chosen from the file.", params={
    "path": {"type": "string", "required": True, "description": "Absolute path to the data file (or OGR/GDAL connection string)"},
    "name": {"type": "string", "required": False, "description": "Layer name; defaults to the file name"},
    "group": {"type": "string", "required": False, "description": "Place the layer inside this layer-tree group (created if missing)"},
}, mutates=True)
def add_layer(path, name=None, group=None):
    if not os.path.exists(path) and "://" not in path:
        raise ToolError("File not found: %s" % path, code="not_found")
    return _load_layer_from_path(path, name, group)


@tool("create_layer", "Create a new empty vector layer file (gpkg or shp) with an attribute schema, and add it to the project. Ready for add_features. Note: GeoJSON cannot store fields without features, so it is not accepted here.", params={
    "name": {"type": "string", "required": True, "description": "Layer name (also the default file name)"},
    "geometry_type": {"type": "string", "required": True, "enum": ["point", "line", "polygon", "multipoint", "multiline", "multipolygon", "table"], "description": "Geometry type; 'table' creates an attribute-only layer"},
    "fields": {"type": "array", "required": True, "description": "Attribute schema: [{\"name\": \"id\", \"type\": \"string\"|\"integer\"|\"real\"}, ...] (may be empty)"},
    "crs": {"type": "string", "required": False, "description": "CRS authid (default EPSG:4326)"},
    "path": {"type": "string", "required": False, "description": "Target file (.gpkg or .shp); defaults to copla_layers/<name>.gpkg in the QGIS profile"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group (created if missing)"},
}, mutates=True)
def create_layer(name, geometry_type, fields, crs=None, path=None, group=None):
    wkb = {
        "point": QgsWkbTypes.Point,
        "line": QgsWkbTypes.LineString,
        "polygon": QgsWkbTypes.Polygon,
        "multipoint": QgsWkbTypes.MultiPoint,
        "multiline": QgsWkbTypes.MultiLineString,
        "multipolygon": QgsWkbTypes.MultiPolygon,
        "table": QgsWkbTypes.NoGeometry,
    }[geometry_type]
    target_srs = QgsCoordinateReferenceSystem(crs or "EPSG:4326")
    if not target_srs.isValid():
        raise ToolError("Invalid CRS: %s" % crs, code="bad_args")
    qfields = QgsFields()
    seen = set()
    for spec in fields:
        if not isinstance(spec, dict) or "name" not in spec:
            raise ToolError("Each field must be an object with 'name' and 'type'", code="bad_args")
        ftype = spec.get("type", "string")
        if ftype not in ("string", "integer", "real"):
            raise ToolError("Field type must be string, integer or real (got %s)" % ftype, code="bad_args")
        fname = str(spec["name"])
        if fname in seen:
            raise ToolError("Duplicate field name '%s'" % fname, code="bad_args")
        seen.add(fname)
        vtype = {"string": QVariant.String, "integer": QVariant.Int, "real": QVariant.Double}[ftype]
        qfields.append(QgsField(fname, vtype))
    if path is None:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_") or "layer"
        path = os.path.join(QgsApplication.qgisSettingsDirPath(), "copla_layers", safe + ".gpkg")
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    driver = {".gpkg": "GPKG", ".shp": "ESRI Shapefile"}.get(ext)
    if driver is None:
        raise ToolError(
            "path must end in .gpkg or .shp (GeoJSON cannot keep a field schema without features)",
            code="bad_args",
        )
    if os.path.exists(path):
        raise ToolError("File already exists: %s" % path, code="io_error")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    uri = {
        "point": "Point",
        "line": "LineString",
        "polygon": "Polygon",
        "multipoint": "MultiPoint",
        "multiline": "MultiLineString",
        "multipolygon": "MultiPolygon",
        "table": "None",
    }[geometry_type]
    mem = QgsVectorLayer("%s?crs=%s" % (uri, target_srs.authid()), name, "memory")
    mem.dataProvider().addAttributes([QgsField(f) for f in qfields])
    mem.updateFields()
    try:
        err = QgsVectorFileWriter.writeAsVectorFormat(mem, path, "UTF-8", target_srs, driver)
    except Exception as exc:
        raise ToolError("Could not create '%s': %s" % (path, exc), code="io_error")
    if isinstance(err, tuple):
        err = err[0]
    if err != QgsVectorFileWriter.NoError or not os.path.exists(path):
        raise ToolError("Could not create '%s' (writer error %s)" % (path, err), code="io_error")
    return _load_layer_from_path(path, name, group)


_CONTENT_TYPE_EXT = {
    "application/geo+json": ".geojson",
    "application/json": ".geojson",
    "application/geopackage+sqlite3": ".gpkg",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "image/tiff": ".tif",
    "image/geotiff": ".tif",
    "application/vnd.google-earth.kml+xml": ".kml",
    "application/vnd.google-earth.kmz": ".kmz",
    "text/csv": ".csv",
    "application/gml+xml": ".gml",
}


def _http_get_bytes(url, max_bytes, timeout_ms=60000):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs are supported", code="bad_args")
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", b"Copla-QGIS-Plugin")
    reply = QgsNetworkAccessManager.instance().get(request)
    loop = QEventLoop()
    state = {"timeout": False, "overflow": False}
    timer = QTimer()
    timer.setSingleShot(True)

    def on_timeout():
        state["timeout"] = True
        reply.abort()
        loop.quit()

    def on_progress(received, total):
        if received > max_bytes or (total and total > max_bytes):
            state["overflow"] = True
            reply.abort()
            loop.quit()

    timer.timeout.connect(on_timeout)
    reply.downloadProgress.connect(on_progress)
    reply.finished.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec_()
    timer.stop()
    for sig, fn in ((reply.downloadProgress, on_progress), (reply.finished, loop.quit), (timer.timeout, on_timeout)):
        try:
            sig.disconnect(fn)
        except (TypeError, RuntimeError):
            pass
    status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    content_type = reply.header(QNetworkRequest.ContentTypeHeader)
    if state["timeout"]:
        reply.deleteLater()
        raise ToolError("Timed out fetching %s" % url, code="io_error")
    if state["overflow"]:
        reply.deleteLater()
        raise ToolError(
            "Response larger than the %d MB limit" % (max_bytes // (1024 * 1024)),
            code="io_error",
        )
    if reply.error() != QNetworkReply.NoError:
        message = reply.errorString()
        reply.deleteLater()
        raise ToolError("HTTP error for %s: %s (status %s)" % (url, message, status), code="io_error")
    data = bytes(reply.readAll())
    reply.deleteLater()
    if status is not None and not (200 <= int(status) < 300):
        raise ToolError("HTTP status %s for %s" % (status, url), code="io_error")
    return data, str(content_type or "")


def _http_post_bytes(url, payload, content_type, headers, max_bytes, timeout_ms=60000):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Only http:// and https:// URLs are supported", code="bad_args")
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", b"Copla-QGIS-Plugin")
    if content_type:
        request.setRawHeader(b"Content-Type", str(content_type).encode("utf-8"))
    for key, value in (headers or {}).items():
        request.setRawHeader(str(key).encode("utf-8"), str(value).encode("utf-8"))
    body = QByteArray(bytes(payload))
    reply = QgsNetworkAccessManager.instance().post(request, body)
    loop = QEventLoop()
    state = {"timeout": False, "overflow": False}
    timer = QTimer()
    timer.setSingleShot(True)

    def on_timeout():
        state["timeout"] = True
        reply.abort()
        loop.quit()

    def on_progress(received, total):
        if received > max_bytes or (total and total > max_bytes):
            state["overflow"] = True
            reply.abort()
            loop.quit()

    timer.timeout.connect(on_timeout)
    reply.downloadProgress.connect(on_progress)
    reply.finished.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec_()
    timer.stop()
    for sig, fn in ((reply.downloadProgress, on_progress), (reply.finished, loop.quit), (timer.timeout, on_timeout)):
        try:
            sig.disconnect(fn)
        except (TypeError, RuntimeError):
            pass
    status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    resp_content_type = reply.header(QNetworkRequest.ContentTypeHeader)
    if state["timeout"]:
        reply.deleteLater()
        raise ToolError("Timed out posting to %s" % url, code="io_error")
    if state["overflow"]:
        reply.deleteLater()
        raise ToolError(
            "Response larger than the %d MB limit" % (max_bytes // (1024 * 1024)),
            code="io_error",
        )
    error = reply.error()
    data = bytes(reply.readAll())
    reply.deleteLater()
    if status is None:
        if error != QNetworkReply.NoError:
            raise ToolError("HTTP error for %s: %s" % (url, _http_error_text(error)), code="io_error")
        return data, str(resp_content_type or ""), None
    return data, str(resp_content_type or ""), int(status)


def _http_error_text(error):
    for const, text in (
        (QNetworkReply.ConnectionRefusedError, "connection refused"),
        (QNetworkReply.RemoteHostClosedError, "remote host closed the connection"),
        (QNetworkReply.HostNotFoundError, "host not found"),
        (QNetworkReply.TimeoutError, "timed out"),
        (QNetworkReply.SslHandshakeFailedError, "TLS handshake failed"),
        (QNetworkReply.OperationCanceledError, "operation cancelled"),
        (QNetworkReply.ProxyConnectionRefusedError, "proxy refused the connection"),
        (QNetworkReply.AuthenticationRequiredError, "authentication required"),
    ):
        if error == const:
            return text
    return "network error (%s)" % int(error)


@tool("download_layer", "Download a geodata file from an http(s) URL into the local Copla cache and add it as a layer. Works with geojson, gpkg, kml, csv, zip (shapefile bundle), tif, ... Max 100 MB.", params={
    "url": {"type": "string", "required": True, "description": "Direct http(s) URL of the data file"},
    "name": {"type": "string", "required": False, "description": "Layer name; defaults to the file name"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group (created if missing)"},
}, mutates=True)
def download_layer(url, name=None, group=None):
    data, content_type = _http_get_bytes(url, max_bytes=100 * 1024 * 1024, timeout_ms=120000)
    if not data:
        raise ToolError("The URL returned an empty body", code="io_error")
    basename = os.path.basename(urllib.parse.urlparse(url).path.rstrip("/")) or "download"
    base, ext = os.path.splitext(basename)
    ext = ext.lower()
    known = {".geojson", ".json", ".gpkg", ".shp", ".kml", ".kmz", ".gml", ".csv", ".zip", ".tif", ".tiff", ".img", ".asc"}
    if ext not in known:
        ctype = content_type.split(";")[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(ctype)
        if ext is None:
            raise ToolError(
                "Cannot determine file type from url or Content-Type '%s'; use add_layer with a local path instead" % content_type,
                code="io_error",
            )
        basename = basename + ext if not os.path.splitext(basename)[1] else base + ext
    cache_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "copla_cache")
    os.makedirs(cache_dir, exist_ok=True)
    target = os.path.join(cache_dir, os.path.basename(basename))
    try:
        with open(target, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise ToolError("Could not write %s: %s" % (target, exc), code="io_error")
    summary = _load_layer_from_path(target, name, group)
    summary["source_url"] = url
    summary["file"] = target
    return summary


@tool("http_get", "Fetch a http(s) URL and return its body as text. Useful for APIs, CSV and JSON endpoints (max 200 KB).", params={
    "url": {"type": "string", "required": True, "description": "http(s) URL to fetch"},
})
def http_get(url):
    data, content_type = _http_get_bytes(url, max_bytes=200 * 1024, timeout_ms=30000)
    return {
        "url": url,
        "content_type": content_type,
        "bytes": len(data),
        "truncated": len(data) >= 200 * 1024,
        "body": data.decode("utf-8", "replace"),
    }


@tool("http_post", "Send an HTTP POST request (JSON body by default) and return status and response text. Useful for APIs that require POST (max 200 KB response).", params={
    "url": {"type": "string", "required": True, "description": "http(s) URL to POST to"},
    "json": {"type": "object", "required": False, "description": "JSON body, sent with Content-Type application/json (not combinable with body)"},
    "body": {"type": "string", "required": False, "description": "Raw text body instead of JSON (not combinable with json)"},
    "headers": {"type": "object", "required": False, "description": "Extra request headers, e.g. {\"Authorization\": \"Bearer ...\"}"},
    "content_type": {"type": "string", "required": False, "description": "Content-Type for a raw body (default text/plain)"},
}, mutates=False)
def http_post(url, json=None, body=None, headers=None, content_type=None):
    if json is not None and body is not None:
        raise ToolError("Pass either json or body, not both", code="bad_args")
    if json is not None:
        payload = _json.dumps(json).encode("utf-8")
        ctype = "application/json"
    else:
        payload = (body or "").encode("utf-8")
        ctype = content_type or "text/plain; charset=utf-8"
    data, resp_ctype, status = _http_post_bytes(
        url, payload, ctype, headers, max_bytes=200 * 1024, timeout_ms=30000
    )
    return {
        "url": url,
        "status": status,
        "ok": bool(status is not None and 200 <= status < 300),
        "content_type": resp_ctype,
        "bytes": len(data),
        "truncated": len(data) >= 200 * 1024,
        "body": data.decode("utf-8", "replace"),
    }


@tool("save_layer_as", "Export a layer to a file. Vector: .gpkg, .geojson, .shp or .kml; raster: .tif/.img/.png (via gdal:translate).", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "path": {"type": "string", "required": True, "description": "Absolute destination path with extension"},
}, mutates=True)
def save_layer_as(layer, path):
    lyr = find_layer(layer)
    ext = os.path.splitext(path)[1].lower()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if isinstance(lyr, QgsVectorLayer):
        drivers = {".gpkg": "GPKG", ".geojson": "GeoJSON", ".json": "GeoJSON", ".shp": "ESRI Shapefile", ".kml": "KML"}
        driver = drivers.get(ext)
        if driver is None:
            raise ToolError("For vector layers path must end in: %s" % ", ".join(drivers), code="bad_args")
        try:
            err = QgsVectorFileWriter.writeAsVectorFormat(lyr, path, "UTF-8", lyr.crs(), driver)
        except Exception as exc:
            raise ToolError("Export failed: %s" % exc, code="io_error")
        if isinstance(err, tuple):
            err = err[0]
        if err != QgsVectorFileWriter.NoError:
            raise ToolError("Export failed with code %s" % err, code="io_error")
    else:
        if ext not in (".tif", ".tiff", ".img", ".png", ".jpg", ".jpeg"):
            raise ToolError("For raster layers path must end in .tif, .img, .png or .jpg", code="bad_args")
        if processing is None:
            raise ToolError("Processing plugin is not available for raster export", code="tool_error")
        try:
            processing.run("gdal:translate", {"INPUT": lyr, "OUTPUT": path})
        except Exception as exc:
            raise ToolError("Raster export failed: %s" % exc, code="run_error")
    if not os.path.exists(path):
        raise ToolError("Export reported success but %s does not exist" % path, code="io_error")
    return {"saved": path, "size": os.path.getsize(path)}


@tool("remove_layer", "Remove a layer from the project (does not delete the file on disk).", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
}, mutates=True)
def remove_layer(layer):
    lyr = find_layer(layer)
    name = lyr.name()
    _project().removeMapLayer(lyr.id())
    return {"removed": name}


@tool("remove_group", "Remove a group from the layer tree, along with the layers and subgroups inside it (they leave the project; files on disk are not deleted).", params={
    "group": {"type": "string", "required": True, "description": "Group name (top level or nested)"},
}, mutates=True)
def remove_group(group):
    root = _project().layerTreeRoot()
    node = root.findGroup(group)
    if node is None:
        raise ToolError("Group not found: %s" % group, code="not_found")
    layer_ids = [l.layer().id() for l in node.findLayers() if l.layer()]
    removed_layers = [l.name() for l in node.findLayers()]
    if layer_ids:
        _project().removeMapLayers(layer_ids)
    node.parent().removeChildNode(node)
    return {"removed": group, "layers": removed_layers}


@tool("create_group", "Create a group (folder) in the layer tree to organize layers. Does nothing if it already exists.", params={
    "name": {"type": "string", "required": True, "description": "New group name"},
}, mutates=True)
def create_group(name):
    root = _project().layerTreeRoot()
    node = root.findGroup(name)
    if node is not None:
        return {"group": node.name(), "created": False}
    node = root.addGroup(name)
    return {"group": node.name(), "created": True}


@tool("rename_group", "Rename a group in the layer tree.", params={
    "group": {"type": "string", "required": True, "description": "Group name (top level or nested)"},
    "name": {"type": "string", "required": True, "description": "New group name"},
}, mutates=True)
def rename_group(group, name):
    root = _project().layerTreeRoot()
    node = root.findGroup(group)
    if node is None:
        raise ToolError("Group not found: %s" % group, code="not_found")
    existing = root.findGroup(name)
    if existing is not None and existing is not node:
        raise ToolError("A group named '%s' already exists" % name, code="io_error")
    old = node.name()
    node.setName(name)
    return {"renamed": old, "to": name}


@tool("move_layer", "Move a layer into a group of the layer tree (the group is created if missing). Layers already in the project are reused; nothing is deleted.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "group": {"type": "string", "required": False, "description": "Destination group name (created if missing); omit to move to the top level"},
}, mutates=True)
def move_layer(layer, group=None):
    lyr = find_layer(layer)
    root = _project().layerTreeRoot()
    node = root.findLayer(lyr)
    if node is None:
        raise ToolError("Layer has no tree node", code="tool_error")
    if group:
        dest = root.findGroup(group)
        if dest is None:
            dest = root.addGroup(group)
    else:
        dest = root
    parent = node.parent()
    dest.addLayer(lyr)
    parent.removeChildNode(node)
    if iface is not None:
        iface.mapCanvas().refresh()
    return {"layer": lyr.name(), "group": group or "(top level)"}


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


@tool("add_field", "Add a new attribute field to a vector layer. The layer must have no pending edits.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "name": {"type": "string", "required": True, "description": "New field name"},
    "type": {"type": "string", "required": True, "enum": ["string", "integer", "real"], "description": "Field type"},
    "length": {"type": "integer", "required": False, "description": "Max characters for string fields (default 80)"},
}, mutates=True)
def add_field(layer, name, type, length=None):
    lyr = _vector_layer(layer, "add_field")
    if lyr.fields().indexFromName(name) >= 0:
        raise ToolError("Field '%s' already exists" % name, code="io_error")
    if lyr.isEditable():
        raise ToolError("Layer has pending edits; commit them first", code="tool_error")
    vtype = {"string": QVariant.String, "integer": QVariant.Int, "real": QVariant.Double}[type]
    field = QgsField(name, vtype)
    if type == "string":
        field.setLength(max(1, int(length) if length else 80))
    if not lyr.dataProvider().addAttributes([field]):
        raise ToolError("The provider rejected the new field", code="tool_error")
    lyr.updateFields()
    return {"layer": lyr.name(), "field": name, "type": type}


@tool("remove_field", "Delete an attribute field from a vector layer. The data in that column is lost. The layer must have no pending edits.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "field": {"type": "string", "required": True, "description": "Field name to delete"},
}, mutates=True)
def remove_field(layer, field):
    lyr = _vector_layer(layer, "remove_field")
    idx = _field_index(lyr, field)
    if lyr.isEditable():
        raise ToolError("Layer has pending edits; commit them first", code="tool_error")
    if not lyr.dataProvider().deleteAttributes([idx]):
        raise ToolError("The provider rejected the field deletion", code="tool_error")
    lyr.updateFields()
    return {"layer": lyr.name(), "removed_field": field}


@tool("rename_field", "Rename an attribute field of a vector layer. The layer must have no pending edits.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "field": {"type": "string", "required": True, "description": "Current field name"},
    "new_name": {"type": "string", "required": True, "description": "New field name"},
}, mutates=True)
def rename_field(layer, field, new_name):
    lyr = _vector_layer(layer, "rename_field")
    idx = _field_index(lyr, field)
    if new_name == field:
        return {"layer": lyr.name(), "renamed": field, "to": new_name}
    if lyr.fields().indexFromName(new_name) >= 0:
        raise ToolError("Field '%s' already exists" % new_name, code="io_error")
    if lyr.isEditable():
        raise ToolError("Layer has pending edits; commit them first", code="tool_error")
    if not lyr.dataProvider().renameAttributes({idx: new_name}):
        raise ToolError("The provider rejected the rename", code="tool_error")
    lyr.updateFields()
    return {"layer": lyr.name(), "renamed": field, "to": new_name}


@tool("calculate_field", "Fill or update an attribute field with the result of a QGIS expression evaluated for each feature. Example: field='area_km2', expression='$area / 1000000'. Edits are committed when done.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name (must be editable)"},
    "field": {"type": "string", "required": True, "description": "Target field (must exist; add it first with add_field)"},
    "expression": {"type": "string", "required": True, "description": "QGIS expression evaluated per feature, e.g. $area / 1000000 or upper(name)"},
    "filter": {"type": "string", "required": False, "description": "Only update features matching this expression"},
}, mutates=True)
def calculate_field(layer, field, expression, filter=None):
    lyr = _vector_layer(layer, "calculate_field")
    idx = _field_index(lyr, field)
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise ToolError("Expression parse error: %s" % expr.parserErrorString(), code="bad_args")
    request = QgsFeatureRequest()
    if filter:
        probe = QgsExpression(filter)
        if probe.hasParserError():
            raise ToolError("Invalid filter expression: %s" % probe.parserErrorString(), code="bad_args")
        request.setFilterExpression(filter)
    if not lyr.startEditing():
        raise ToolError("Layer '%s' is not editable" % lyr.name(), code="tool_error")
    updated = 0
    base_context = lyr.createExpressionContext()
    try:
        for feat in lyr.getFeatures(request):
            context = QgsExpressionContext(base_context)
            context.setFeature(feat)
            value = expr.evaluate(context)
            if expr.hasEvalError():
                raise ToolError("Expression eval error: %s" % expr.evalErrorString(), code="tool_error")
            if not lyr.changeAttributeValue(feat.id(), idx, value):
                raise ToolError("QGIS rejected the value on feature %s" % feat.id(), code="tool_error")
            updated += 1
        if not lyr.commitChanges():
            raise ToolError("Could not commit the changes", code="tool_error")
    except Exception:
        lyr.rollBack()
        raise
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "field": field, "updated": updated}


def _norm_attr(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    text = str(value)
    return None if text == "<NULL>" else text


@tool("unique_values", "List the distinct values of a field with their counts (most frequent first). Useful before using a categorized renderer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "field": {"type": "string", "required": True, "description": "Field name"},
    "limit": {"type": "integer", "required": False, "description": "Max values to return (default 200, max 1000)"},
})
def unique_values(layer, field, limit=200):
    lyr = _vector_layer(layer, "unique_values")
    idx = _field_index(lyr, field)
    limit = max(1, min(int(limit), 1000))
    request = QgsFeatureRequest().setSubsetOfAttributes([idx])
    counts = {}
    for feat in lyr.getFeatures(request):
        value = _norm_attr(feat[field])
        counts[value] = counts.get(value, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    out = [{"value": v, "count": c} for v, c in items[:limit]]
    return {
        "layer": lyr.name(),
        "field": field,
        "distinct": len(counts),
        "returned": len(out),
        "values": out,
    }


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


def _style_result(result):
    if isinstance(result, tuple):
        message = str(result[0]) if result else ""
        ok = bool(result[1]) if len(result) > 1 else True
        return ok, message
    return True, ""


@tool("save_style", "Save a layer's style (renderer, labels, symbols) to a .qml file.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "path": {"type": "string", "required": True, "description": "Destination .qml file (parent folders are created)"},
}, mutates=True)
def save_style(layer, path):
    lyr = find_layer(layer)
    if os.path.splitext(path)[1].lower() != ".qml":
        raise ToolError("path must end in .qml", code="bad_args")
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    ok, message = _style_result(lyr.saveNamedStyle(path))
    if not ok:
        raise ToolError("Could not save style: %s" % message, code="io_error")
    if not os.path.exists(path):
        raise ToolError("Style file was not written: %s" % path, code="io_error")
    return {"layer": lyr.name(), "saved": os.path.abspath(path)}


@tool("load_style", "Apply a .qml style file to a layer.", params={
    "layer": {"type": "string", "required": True, "description": "Layer id or name"},
    "path": {"type": "string", "required": True, "description": "Path to an existing .qml file"},
}, mutates=True)
def load_style(layer, path):
    lyr = find_layer(layer)
    if not os.path.isfile(path):
        raise ToolError("Style file not found: %s" % path, code="not_found")
    ok, message = _style_result(lyr.loadNamedStyle(path))
    if not ok:
        raise ToolError("Could not load style: %s" % message, code="io_error")
    if iface is not None:
        lyr.triggerRepaint()
    return {"layer": lyr.name(), "loaded": os.path.abspath(path)}


@tool("copy_style", "Copy the style (renderer, labels) from one layer to another.", params={
    "source": {"type": "string", "required": True, "description": "Layer id or name whose style is copied"},
    "target": {"type": "string", "required": True, "description": "Layer id or name that receives the style"},
}, mutates=True)
def copy_style(source, target):
    src = find_layer(source)
    dst = find_layer(target)
    if src.id() == dst.id():
        raise ToolError("source and target are the same layer", code="bad_args")
    fd, tmp = tempfile.mkstemp(suffix=".qml", prefix="copla_style_")
    os.close(fd)
    try:
        ok, message = _style_result(src.saveNamedStyle(tmp))
        if not ok or not os.path.exists(tmp):
            raise ToolError("Could not read the source style: %s" % message, code="io_error")
        ok, message = _style_result(dst.loadNamedStyle(tmp))
        if not ok:
            raise ToolError("Could not apply the style: %s" % message, code="io_error")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if iface is not None:
        dst.triggerRepaint()
    return {"from": src.name(), "to": dst.name()}


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


_SELECT_PREDICATES = {
    "intersects": 0,
    "contains": 1,
    "disjoint": 2,
    "equals": 3,
    "touches": 4,
    "overlaps": 5,
    "within": 6,
    "crosses": 7,
}
_SELECT_METHODS = {"new": 0, "add": 1, "within": 2, "remove": 3}


@tool("select_by_location", "Select features of a layer based on their spatial relationship with another layer (like the 'Select by location' toolbox). The selection stays visible in the QGIS UI.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name whose features will be selected"},
    "other": {"type": "string", "required": True, "description": "Reference vector layer to test against"},
    "predicate": {"type": "string", "required": False, "enum": ["intersects", "contains", "disjoint", "equals", "touches", "overlaps", "within", "crosses"], "description": "Spatial relationship (default intersects)"},
    "method": {"type": "string", "required": False, "enum": ["new", "add", "within", "remove"], "description": "new = replace selection, add = add to current, within = keep only what is already selected, remove = deselect matching (default new)"},
}, mutates=True)
def select_by_location(layer, other, predicate="intersects", method="new"):
    if processing is None:
        raise ToolError("Processing plugin is not available", code="tool_error")
    lyr = _vector_layer(layer, "select_by_location")
    ref = _vector_layer(other, "select_by_location")
    if not lyr.isSpatial():
        raise ToolError("Layer '%s' has no geometry" % lyr.name(), code="bad_args")
    if not ref.isSpatial():
        raise ToolError("Reference layer '%s' has no geometry" % ref.name(), code="bad_args")
    feedback = _CollectFeedback()
    try:
        processing.run("native:selectbylocation", {
            "INPUT": lyr,
            "PREDICATE": [_SELECT_PREDICATES[predicate]],
            "INTERSECT": ref,
            "METHOD": _SELECT_METHODS[method],
        }, feedback=feedback)
    except Exception as exc:
        raise ToolError(
            "selectbylocation failed: %s\n%s" % (exc, "\n".join(feedback.lines[-20:])),
            code="run_error",
        )
    if iface is not None:
        iface.mapCanvas().refresh()
    return {"layer": lyr.name(), "predicate": predicate, "selected": lyr.selectedFeatureCount()}


@tool("zoom_to_project", "Pan and zoom the map canvas so all layers of the project fit in view.", mutates=True)
def zoom_to_project():
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    project = _project()
    dest = project.crs()
    union = None
    for lyr in project.mapLayers().values():
        extent = lyr.extent()
        if extent is None or extent.isNull():
            continue
        if dest.isValid() and lyr.crs().isValid() and lyr.crs() != dest:
            extent = QgsCoordinateTransform(lyr.crs(), dest, project).transformBoundingBox(extent)
        if union is None:
            union = QgsRectangle(extent)
        else:
            union.combineExtentWith(extent)
    if union is None:
        raise ToolError("The project has no layers with an extent to zoom to", code="tool_error")
    union.grow(max(union.width(), union.height()) * 0.05)
    canvas = iface.mapCanvas()
    canvas.setExtent(union)
    canvas.refresh()
    return {"extent": _extent_list(canvas.extent())}


# ---------------------------------------------------------------- files

@tool("list_directory", "List files and folders inside a directory (name, kind, size). Max 500 entries.", params={
    "path": {"type": "string", "required": True, "description": "Absolute directory path"},
})
def list_directory(path):
    if not os.path.isdir(path):
        raise ToolError("Not a directory: %s" % path, code="not_found")
    entries = []
    truncated = False
    for i, entry in enumerate(sorted(os.listdir(path))):
        if i >= 500:
            truncated = True
            break
        full = os.path.join(path, entry)
        is_dir = os.path.isdir(full)
        size = None
        if not is_dir:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = None
        entries.append({"name": entry, "kind": "dir" if is_dir else "file", "size": size})
    return {"path": path, "count": len(entries), "truncated": truncated, "entries": entries}


@tool("move_file", "Move or rename a file or folder on disk. Creates destination folders as needed; the destination must not exist.", params={
    "src": {"type": "string", "required": True, "description": "Absolute path of the source file or folder"},
    "dst": {"type": "string", "required": True, "description": "Absolute destination path"},
}, mutates=True)
def move_file(src, dst):
    if not os.path.exists(src):
        raise ToolError("Source not found: %s" % src, code="not_found")
    dst = os.path.abspath(dst)
    if os.path.exists(dst):
        raise ToolError("Destination already exists: %s" % dst, code="io_error")
    parent = os.path.dirname(dst)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ToolError("Could not create %s: %s" % (parent, exc), code="io_error")
    try:
        shutil.move(src, dst)
    except (OSError, shutil.Error) as exc:
        raise ToolError("Move failed: %s" % exc, code="io_error")
    return {"moved": src, "to": dst}


@tool("read_file", "Read a text file from disk and return its content (text/UTF-8 only, max 200 KB).", params={
    "path": {"type": "string", "required": True, "description": "Absolute path of the text file"},
    "encoding": {"type": "string", "required": False, "description": "Text encoding (default utf-8)"},
    "max_bytes": {"type": "integer", "required": False, "description": "Max bytes to read (default 204800, max 1048576)"},
})
def read_file(path, encoding=None, max_bytes=None):
    if not os.path.isfile(path):
        raise ToolError("File not found: %s" % path, code="not_found")
    limit = max(1, min(int(max_bytes) if max_bytes else 200 * 1024, 1024 * 1024))
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        raise ToolError("Could not read %s: %s" % (path, exc), code="io_error")
    truncated = len(data) > limit
    data = data[:limit]
    if b"\x00" in data[:4096]:
        raise ToolError("'%s' looks like a binary file; read_file only handles text" % path, code="bad_args")
    try:
        text = data.decode(encoding or "utf-8")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ToolError("Could not decode the file as %s: %s" % (encoding or "utf-8", exc), code="io_error")
    return {"path": os.path.abspath(path), "bytes": len(data), "truncated": truncated, "content": text}


@tool("file_info", "Get metadata about a file or folder: kind, size, extension and last modification time.", params={
    "path": {"type": "string", "required": True, "description": "Absolute path"},
})
def file_info(path):
    if not os.path.exists(path):
        raise ToolError("Not found: %s" % path, code="not_found")
    is_dir = os.path.isdir(path)
    st = os.stat(path)
    return {
        "path": os.path.abspath(path),
        "kind": "dir" if is_dir else "file",
        "size": None if is_dir else st.st_size,
        "ext": "" if is_dir else os.path.splitext(path)[1].lower(),
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
    }


@tool("copy_file", "Copy a file or folder on disk. Creates destination folders as needed; the destination must not exist.", params={
    "src": {"type": "string", "required": True, "description": "Absolute path of the source file or folder"},
    "dst": {"type": "string", "required": True, "description": "Absolute destination path"},
}, mutates=True)
def copy_file(src, dst):
    if not os.path.exists(src):
        raise ToolError("Source not found: %s" % src, code="not_found")
    dst = os.path.abspath(dst)
    if os.path.exists(dst):
        raise ToolError("Destination already exists: %s" % dst, code="io_error")
    parent = os.path.dirname(dst)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ToolError("Could not create %s: %s" % (parent, exc), code="io_error")
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    except (OSError, shutil.Error) as exc:
        raise ToolError("Copy failed: %s" % exc, code="io_error")
    return {"copied": os.path.abspath(src), "to": dst}


@tool("delete_file", "Delete a file (or a folder with recursive=true) from disk. This cannot be undone.", params={
    "path": {"type": "string", "required": True, "description": "Absolute path to delete"},
    "recursive": {"type": "boolean", "required": False, "description": "Required to delete a folder and its contents (default false)"},
}, mutates=True)
def delete_file(path, recursive=False):
    target = os.path.abspath(path)
    if not os.path.exists(target):
        raise ToolError("Not found: %s" % target, code="not_found")
    if os.path.dirname(target) == target:
        raise ToolError("Refusing to delete a filesystem root", code="bad_args")
    profile = os.path.abspath(QgsApplication.qgisSettingsDirPath())
    if target == profile:
        raise ToolError("Refusing to delete the QGIS profile directory", code="bad_args")
    is_dir = os.path.isdir(target)
    if is_dir and not recursive:
        raise ToolError(
            "'%s' is a folder; pass recursive=true to delete it and its contents" % target,
            code="bad_args",
        )
    try:
        if is_dir:
            shutil.rmtree(target)
        else:
            os.remove(target)
    except OSError as exc:
        raise ToolError("Delete failed: %s" % exc, code="io_error")
    return {"deleted": target, "kind": "dir" if is_dir else "file"}


@tool("download_file", "Download a http(s) URL to a local file (max 100 MB). The destination must not exist.", params={
    "url": {"type": "string", "required": True, "description": "Direct http(s) URL"},
    "path": {"type": "string", "required": True, "description": "Absolute destination path"},
}, mutates=True)
def download_file(url, path):
    data, content_type = _http_get_bytes(url, max_bytes=100 * 1024 * 1024, timeout_ms=120000)
    if not data:
        raise ToolError("The URL returned an empty body", code="io_error")
    path = os.path.abspath(path)
    if os.path.exists(path):
        raise ToolError("Destination already exists: %s" % path, code="io_error")
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ToolError("Could not create %s: %s" % (parent, exc), code="io_error")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise ToolError("Could not write %s: %s" % (path, exc), code="io_error")
    return {"saved": path, "bytes": len(data), "content_type": content_type}


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


def _alg_output_path(name):
    cache = os.path.join(QgsApplication.qgisSettingsDirPath(), "copla_cache")
    os.makedirs(cache, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_") or "result"
    path = os.path.join(cache, safe + ".gpkg")
    if os.path.exists(path):
        stamp = int(time.time())
        path = os.path.join(cache, "%s_%d.gpkg" % (safe, stamp))
        n = 1
        while os.path.exists(path):
            path = os.path.join(cache, "%s_%d_%d.gpkg" % (safe, stamp, n))
            n += 1
    return path


def _run_alg_to_layer(alg_id, params, name, group=None):
    if processing is None:
        raise ToolError("Processing plugin is not available", code="tool_error")
    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    if alg is None:
        raise ToolError("Algorithm not found: %s" % alg_id, code="tool_error")
    target = _alg_output_path(name)
    run_params = dict(params)
    run_params["OUTPUT"] = target
    feedback = _CollectFeedback()
    try:
        processing.run(alg_id, run_params, feedback=feedback)
    except Exception as exc:
        raise ToolError(
            "Algorithm failed: %s\n%s" % (exc, "\n".join(feedback.lines[-20:])),
            code="run_error",
        )
    if not os.path.exists(target):
        raise ToolError("Algorithm finished but %s was not created" % target, code="run_error")
    return _load_layer_from_path(target, name, group)


@tool("buffer", "Buffer vector features by a distance and add the result as a new layer. distance is in the layer CRS units: use meters only if the layer CRS uses meters.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "distance": {"type": "number", "required": True, "description": "Buffer distance in layer CRS units"},
    "segments": {"type": "integer", "required": False, "description": "Segments per quarter circle (default 5)"},
    "dissolve": {"type": "boolean", "required": False, "description": "Merge overlapping buffers into one feature (default false)"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_buffer')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def buffer(layer, distance, segments=5, dissolve=False, name=None, group=None):
    lyr = _vector_layer(layer, "buffer")
    params = {
        "INPUT": lyr,
        "DISTANCE": float(distance),
        "SEGMENTS": int(segments),
        "DISSOLVE": bool(dissolve),
    }
    return _run_alg_to_layer("native:buffer", params, name or (lyr.name() + "_buffer"), group)


@tool("reproject_layer", "Reproject a vector layer to another CRS and add the result as a new layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "crs": {"type": "string", "required": True, "description": "Target CRS, e.g. EPSG:3857"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_<crs>')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def reproject_layer(layer, crs, name=None, group=None):
    lyr = _vector_layer(layer, "reproject_layer")
    target = QgsCoordinateReferenceSystem(crs)
    if not target.isValid():
        raise ToolError("Invalid CRS: %s" % crs, code="bad_args")
    suffix = target.authid().replace(":", "").replace("/", "_") or "reproj"
    return _run_alg_to_layer(
        "native:reprojectlayer",
        {"INPUT": lyr, "TARGET_CRS": target},
        name or ("%s_%s" % (lyr.name(), suffix)),
        group,
    )


@tool("clip", "Cut a vector layer with a polygon overlay layer (keep only what falls inside) and add the result as a new layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer to clip"},
    "overlay": {"type": "string", "required": True, "description": "Polygon layer used as the cookie cutter (vector id or name)"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_clip')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def clip(layer, overlay, name=None, group=None):
    lyr = _vector_layer(layer, "clip")
    ov = _vector_layer(overlay, "clip")
    params = {"INPUT": lyr, "OVERLAY": ov}
    return _run_alg_to_layer("native:clip", params, name or (lyr.name() + "_clip"), group)


@tool("intersection", "Intersect two vector layers (keep parts of 'layer' that overlap 'overlay', with attributes of both) and add the result as a new layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "overlay": {"type": "string", "required": True, "description": "Vector layer id or name to intersect with"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_intersection')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def intersection(layer, overlay, name=None, group=None):
    lyr = _vector_layer(layer, "intersection")
    ov = _vector_layer(overlay, "intersection")
    params = {"INPUT": lyr, "OVERLAY": ov}
    return _run_alg_to_layer("native:intersection", params, name or (lyr.name() + "_intersection"), group)


@tool("dissolve", "Merge features of a vector layer into one (optionally one result per value of a field) and add the result as a new layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "field": {"type": "string", "required": False, "description": "Group by this field (each unique value becomes one feature); omit to merge everything into one"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_dissolve')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def dissolve(layer, field=None, name=None, group=None):
    lyr = _vector_layer(layer, "dissolve")
    params = {"INPUT": lyr}
    if field:
        _field_index(lyr, field)
        params["FIELD"] = [field]
    return _run_alg_to_layer("native:dissolve", params, name or (lyr.name() + "_dissolve"), group)


@tool("fix_geometries", "Repair invalid geometries of a vector layer and add the result as a new layer.", params={
    "layer": {"type": "string", "required": True, "description": "Vector layer id or name"},
    "name": {"type": "string", "required": False, "description": "Result layer name (default '<layer>_fixed')"},
    "group": {"type": "string", "required": False, "description": "Layer-tree group for the result (created if missing)"},
}, mutates=True)
def fix_geometries(layer, name=None, group=None):
    lyr = _vector_layer(layer, "fix_geometries")
    return _run_alg_to_layer("native:fixgeometries", {"INPUT": lyr}, name or (lyr.name() + "_fixed"), group)


# ---------------------------------------------------------------- rendering

@tool("render_map", "Render the current map view to an image file (png/jpg). Optionally set size, extent and/or output CRS without changing the user's view permanently.", params={
    "output_path": {"type": "string", "required": True, "description": "Destination file, e.g. C:/maps/out.png"},
    "width": {"type": "integer", "required": False, "description": "Image width in px (default 1600)"},
    "height": {"type": "integer", "required": False, "description": "Image height in px (default 1000)"},
    "extent": {"type": "array", "required": False, "description": "[xmin, ymin, xmax, ymax] to render instead of the current view. Geographic degrees (e.g. [-180, -85, 180, 85] for the whole world) or project CRS meters"},
    "crs": {"type": "string", "required": False, "description": "Output CRS, e.g. EPSG:4326 (default: current project CRS)"},
}, mutates=False)
def render_map(output_path, width=1600, height=1000, extent=None, crs=None):
    if iface is None:
        raise ToolError("No GUI available (headless session)", code="tool_error")
    canvas = iface.mapCanvas()
    settings = canvas.mapSettings()
    settings.setOutputSize(QSize(int(width), int(height)))
    if crs:
        out_crs = QgsCoordinateReferenceSystem(crs)
        if not out_crs.isValid():
            raise ToolError("Invalid CRS: %s" % crs, code="bad_args")
        settings.setDestinationCrs(out_crs)
    if extent is not None:
        rect = _rect_from(extent)
        target = settings.destinationCrs()
        if (
            abs(rect.xMinimum()) <= 180.0
            and abs(rect.xMaximum()) <= 180.0
            and abs(rect.yMinimum()) <= 90.0
            and abs(rect.yMaximum()) <= 90.0
            and target.isValid()
            and target.authid() != "EPSG:4326"
        ):
            rect = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"), target, _project()
            ).transformBoundingBox(rect)
        settings.setExtent(rect)
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
    for layout in _project().layoutManager().printLayouts():
        pages = []
        for i in range(layout.pageCollection().pageCount()):
            page = layout.pageCollection().page(i)
            pages.append({"width_mm": page.pageSize().width(), "height_mm": page.pageSize().height()})
        out.append({"name": layout.name(), "pages": pages})
    return {"count": len(out), "layouts": out}


@tool("create_layout", "Create a print layout with a page and a map item showing the current view. Export it later with export_layout.", params={
    "name": {"type": "string", "required": True, "description": "New layout name (must be unique)"},
    "width_mm": {"type": "integer", "required": False, "description": "Page width in mm (default 210, A4 portrait)"},
    "height_mm": {"type": "integer", "required": False, "description": "Page height in mm (default 297, A4 portrait)"},
}, mutates=True)
def create_layout(name, width_mm=210, height_mm=297):
    project = _project()
    manager = project.layoutManager()
    for existing in manager.printLayouts():
        if existing.name() == name:
            raise ToolError("Layout '%s' already exists" % name, code="io_error")
    w, h = float(width_mm), float(height_mm)
    if w < 10 or h < 10 or w > 5000 or h > 5000:
        raise ToolError("width_mm and height_mm must be between 10 and 5000", code="bad_args")
    layout = QgsPrintLayout(project)
    layout.setName(name)
    manager.addLayout(layout)
    layout.initializeDefaults()
    pages = layout.pageCollection()
    if pages.pageCount() > 0:
        pages.page(0).setPageSize(QgsLayoutSize(w, h))
    margin = 10.0
    map_item = QgsLayoutItemMap(layout)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(margin, margin))
    map_item.attemptResize(QgsLayoutSize(w - 2 * margin, h - 2 * margin))
    if iface is not None:
        extent = iface.mapCanvas().extent()
        if extent is not None and not extent.isNull():
            map_item.setExtent(extent)
    return {"layout": name, "page_mm": [w, h], "items": len(layout.items())}


@tool("export_layout", "Export a print layout to PDF or an image (png/jpg) using the layout's own page size. Use list_layouts for names.", params={
    "name": {"type": "string", "required": True, "description": "Layout name"},
    "output_path": {"type": "string", "required": True, "description": "Destination file (.pdf, .png or .jpg)"},
    "dpi": {"type": "integer", "required": False, "description": "Resolution for image export (default 300)"},
}, mutates=False)
def export_layout(name, output_path, dpi=300):
    layout = None
    for candidate in _project().layoutManager().printLayouts():
        if candidate.name() == name:
            layout = candidate
            break
    if layout is None:
        raise ToolError(
            "Layout '%s' not found. Available: %s"
            % (name, ", ".join(l.name() for l in _project().layoutManager().printLayouts()) or "(none)"),
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
