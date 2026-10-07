"""copla: MCP server exposing a live QGIS session as typed tools.

Safety model: every tool maps 1:1 to a validated operation implemented
inside the QGIS plugin. There is intentionally no execute-python tool.
"""

from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import ALGORITHM_TIMEOUT, CoplaClient, CoplaError

mcp = MCPServer(
    "copla",
    instructions=(
        "Tools for controlling a live QGIS Desktop session. Start with diagnose() "
        "if a call fails, and list_layers() to get layer ids for other tools. "
        "All paths must be absolute. Nothing here executes arbitrary Python."
    ),
)

_client: Optional[CoplaClient] = None


def client() -> CoplaClient:
    global _client
    if _client is None:
        _client = CoplaClient()
    return _client


def _call(tool: str, args: dict[str, Any], timeout: int | None = None) -> Any:
    try:
        if timeout is None:
            return client().call(tool, args)
        return client().call(tool, args, timeout=timeout)
    except CoplaError as exc:
        raise ToolError(str(exc)) from exc


# ------------------------------------------------------------------ health

@mcp.tool()
def diagnose() -> dict:
    """Health check: QGIS version, plugin version, Processing availability and project state. Call first if anything fails."""
    return _call("diagnose", {})


@mcp.tool()
def list_qgis_tools() -> list[dict]:
    """List every tool available on the QGIS side with parameter schemas (raw bridge catalog)."""
    try:
        return client().list_tools()
    except CoplaError as exc:
        raise ToolError(str(exc)) from exc


# ------------------------------------------------------------------ project

@mcp.tool()
def get_project_info() -> dict:
    """Current QGIS project: file path, title, CRS, extent, layer count and groups."""
    return _call("get_project_info", {})


@mcp.tool()
def load_project(path: str) -> dict:
    """Open a QGIS project file (.qgz or .qgs), replacing the current project."""
    return _call("load_project", {"path": path})


@mcp.tool()
def save_project(path: str | None = None) -> dict:
    """Save the project. Omit path to overwrite the current file; provide it to save a copy."""
    return _call("save_project", {"path": path} if path else {})


@mcp.tool()
def set_project_crs(crs: str) -> dict:
    """Set the project CRS, e.g. 'EPSG:4326'. Layers reproject on the fly."""
    return _call("set_project_crs", {"crs": crs})


# ------------------------------------------------------------------ layers

@mcp.tool()
def list_layers() -> list[dict]:
    """List project layers with id, name, type, CRS, visibility and extent. Use 'id' as the layer argument of other tools."""
    return _call("list_layers", {})


@mcp.tool()
def get_layer_info(layer: str) -> dict:
    """Fields, geometry, CRS, extent, renderer and provider for one layer (id or name)."""
    return _call("get_layer_info", {"layer": layer})


@mcp.tool()
def add_layer(path: str, name: str | None = None, group: str | None = None) -> dict:
    """Add a vector (shp, geojson, gpkg, ...) or raster (tif, img, ...) file as a layer. Absolute path required."""
    args: dict[str, Any] = {"path": path}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("add_layer", args)


@mcp.tool()
def create_layer(
    name: str,
    geometry_type: str,
    fields: list[dict],
    crs: str | None = None,
    path: str | None = None,
    group: str | None = None,
) -> dict:
    """Create a new empty vector layer file (gpkg or shp) with an attribute schema, ready for add_features. fields=[{"name": "id", "type": "string"|"integer"|"real"}]."""
    args: dict[str, Any] = {
        "name": name,
        "geometry_type": geometry_type,
        "fields": fields,
    }
    if crs:
        args["crs"] = crs
    if path:
        args["path"] = path
    if group:
        args["group"] = group
    return _call("create_layer", args)


@mcp.tool()
def download_layer(url: str, name: str | None = None, group: str | None = None) -> dict:
    """Download a geodata file (geojson, gpkg, kml, csv, zip, tif...) from an http(s) URL into the local cache and add it as a layer. Max 100 MB."""
    args: dict[str, Any] = {"url": url}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("download_layer", args)


@mcp.tool()
def http_get(url: str) -> dict:
    """Fetch an http(s) URL and return its body as text (max 200 KB). For APIs, CSV and JSON endpoints."""
    return _call("http_get", {"url": url})


@mcp.tool()
def http_post(
    url: str,
    json: dict | None = None,
    body: str | None = None,
    headers: dict | None = None,
    content_type: str | None = None,
) -> dict:
    """Send an HTTP POST request (JSON body by default) and return status and response text (max 200 KB)."""
    args: dict[str, Any] = {"url": url}
    if json is not None:
        args["json"] = json
    if body is not None:
        args["body"] = body
    if headers:
        args["headers"] = headers
    if content_type:
        args["content_type"] = content_type
    return _call("http_post", args)


@mcp.tool()
def save_layer_as(layer: str, path: str) -> dict:
    """Export a layer to a file: vector to .gpkg/.geojson/.shp/.kml, raster to .tif/.img/.png."""
    return _call("save_layer_as", {"layer": layer, "path": path})


@mcp.tool()
def remove_layer(layer: str) -> dict:
    """Remove a layer from the project (the file on disk is kept)."""
    return _call("remove_layer", {"layer": layer})


@mcp.tool()
def remove_group(group: str) -> dict:
    """Remove a layer group from the project, along with its layers and subgroups (files on disk are kept)."""
    return _call("remove_group", {"group": group})


@mcp.tool()
def rename_layer(layer: str, name: str) -> dict:
    """Rename a layer."""
    return _call("rename_layer", {"layer": layer, "name": name})


@mcp.tool()
def create_group(name: str) -> dict:
    """Create a group (folder) in the layer tree; does nothing if it already exists."""
    return _call("create_group", {"name": name})


@mcp.tool()
def rename_group(group: str, name: str) -> dict:
    """Rename a layer-tree group."""
    return _call("rename_group", {"group": group, "name": name})


@mcp.tool()
def move_layer(layer: str, group: str | None = None) -> dict:
    """Move a layer into a layer-tree group (created if missing); omit group to move it to the top level."""
    args: dict[str, Any] = {"layer": layer}
    if group:
        args["group"] = group
    return _call("move_layer", args)


@mcp.tool()
def set_layer_visibility(layer: str, visible: bool) -> dict:
    """Show or hide a layer on the map."""
    return _call("set_layer_visibility", {"layer": layer, "visible": visible})


@mcp.tool()
def zoom_to_layer(layer: str) -> dict:
    """Pan and zoom the QGIS map canvas to a layer's extent."""
    return _call("zoom_to_layer", {"layer": layer})


@mcp.tool()
def add_basemap(style: str = "osm", url: str | None = None, name: str | None = None) -> dict:
    """Add an XYZ basemap (style='osm'|'satellite') or a custom XYZ tile URL with {z}/{x}/{y} placeholders, at the bottom of the layer list."""
    args: dict[str, Any] = {"style": style}
    if url:
        args["url"] = url
    if name:
        args["name"] = name
    return _call("add_basemap", args)


# ------------------------------------------------------------------ features

@mcp.tool()
def get_features(
    layer: str,
    limit: int = 50,
    filter: str | None = None,
    fields: list[str] | None = None,
    include_geometry: bool = False,
) -> dict:
    """Read features as JSON records with optional expression filter and field selection (max 1000 rows)."""
    args: dict[str, Any] = {"layer": layer, "limit": limit, "include_geometry": include_geometry}
    if filter:
        args["filter"] = filter
    if fields:
        args["fields"] = fields
    return _call("get_features", args)


@mcp.tool()
def select_features(layer: str, expression: str) -> dict:
    """Select vector features matching a QGIS expression; the selection stays visible in QGIS."""
    return _call("select_features", {"layer": layer, "expression": expression})


@mcp.tool()
def run_expression(
    layer: str,
    expression: str,
    feature_id: int | None = None,
    aggregate: dict | None = None,
) -> dict:
    """Evaluate a QGIS expression on one feature (feature_id) or aggregate a layer (e.g. aggregate={"function":"sum","expression":area})."""
    args: dict[str, Any] = {"layer": layer, "expression": expression}
    if feature_id is not None:
        args["feature_id"] = feature_id
    if aggregate:
        args["aggregate"] = aggregate
    return _call("run_expression", args)


@mcp.tool()
def add_features(layer: str, features: list[dict]) -> dict:
    """Add features to a vector layer. Each item: {"geometry": "WKT", "attributes": {"field": value}}. Geometry optional for table layers; max 500 per call. Edits are committed."""
    return _call("add_features", {"layer": layer, "features": features})


@mcp.tool()
def delete_features(
    layer: str,
    expression: str | None = None,
    feature_ids: list[int] | None = None,
) -> dict:
    """Delete features from a vector layer by QGIS expression or by feature ids (exactly one of the two). Committed when done."""
    args: dict[str, Any] = {"layer": layer}
    if expression:
        args["expression"] = expression
    if feature_ids:
        args["feature_ids"] = feature_ids
    return _call("delete_features", args)


@mcp.tool()
def update_attributes(
    layer: str,
    values: dict,
    expression: str | None = None,
    feature_ids: list[int] | None = None,
) -> dict:
    """Update attribute values on existing features selected by expression or feature ids, e.g. values={"pop": 1200}."""
    args: dict[str, Any] = {"layer": layer, "values": values}
    if expression:
        args["expression"] = expression
    if feature_ids:
        args["feature_ids"] = feature_ids
    return _call("update_attributes", args)


@mcp.tool()
def add_field(layer: str, name: str, type: str, length: int | None = None) -> dict:
    """Add a new attribute field to a vector layer (type: 'string', 'integer' or 'real'). Layer must have no pending edits."""
    args: dict[str, Any] = {"layer": layer, "name": name, "type": type}
    if length is not None:
        args["length"] = length
    return _call("add_field", args)


@mcp.tool()
def remove_field(layer: str, field: str) -> dict:
    """Delete an attribute field from a vector layer (the column data is lost). Layer must have no pending edits."""
    return _call("remove_field", {"layer": layer, "field": field})


@mcp.tool()
def rename_field(layer: str, field: str, new_name: str) -> dict:
    """Rename an attribute field of a vector layer. Layer must have no pending edits."""
    return _call("rename_field", {"layer": layer, "field": field, "new_name": new_name})


@mcp.tool()
def calculate_field(layer: str, field: str, expression: str, filter: str | None = None) -> dict:
    """Fill or update an attribute field with a QGIS expression evaluated per feature, e.g. expression='$area / 1000000'. Committed when done."""
    args: dict[str, Any] = {"layer": layer, "field": field, "expression": expression}
    if filter:
        args["filter"] = filter
    return _call("calculate_field", args)


@mcp.tool()
def unique_values(layer: str, field: str, limit: int = 200) -> dict:
    """List the distinct values of a field with counts (most frequent first)."""
    return _call("unique_values", {"layer": layer, "field": field, "limit": limit})


# ------------------------------------------------------------------ style

@mcp.tool()
def set_renderer(
    layer: str,
    type: str,
    field: str | None = None,
    color: str | None = None,
    colors: list[str] | None = None,
    ramp: str | None = None,
    classes: int | None = None,
) -> dict:
    """Set vector symbology: type='single' (one color), 'categorized' (unique values of field) or 'graduated' (numeric classes with a color ramp such as 'Spectral' or 'Blues')."""
    args: dict[str, Any] = {"layer": layer, "type": type}
    if field:
        args["field"] = field
    if color:
        args["color"] = color
    if colors:
        args["colors"] = colors
    if ramp:
        args["ramp"] = ramp
    if classes is not None:
        args["classes"] = classes
    return _call("set_renderer", args)


@mcp.tool()
def set_labels(
    layer: str,
    enabled: bool,
    field: str | None = None,
    size: float = 10.0,
    color: str = "#000000",
    halo: bool = True,
    expression: bool = False,
) -> dict:
    """Configure labels on a vector layer: enabled=false hides them; enabling requires field (or expression), with optional size, color and white halo."""
    args: dict[str, Any] = {"layer": layer, "enabled": enabled}
    if field:
        args["field"] = field
    if size is not None:
        args["size"] = size
    if color:
        args["color"] = color
    if halo is not None:
        args["halo"] = halo
    if expression:
        args["expression"] = expression
    return _call("set_labels", args)


@mcp.tool()
def save_style(layer: str, path: str) -> dict:
    """Save a layer's style (renderer, labels, symbols) to a .qml file."""
    return _call("save_style", {"layer": layer, "path": path})


@mcp.tool()
def load_style(layer: str, path: str) -> dict:
    """Apply a .qml style file to a layer."""
    return _call("load_style", {"layer": layer, "path": path})


@mcp.tool()
def copy_style(source: str, target: str) -> dict:
    """Copy the style (renderer, labels) from one layer to another."""
    return _call("copy_style", {"source": source, "target": target})


# ------------------------------------------------------------------ files

@mcp.tool()
def list_directory(path: str) -> dict:
    """List files and folders inside a directory (name, kind, size). Max 500 entries."""
    return _call("list_directory", {"path": path})


@mcp.tool()
def move_file(src: str, dst: str) -> dict:
    """Move or rename a file or folder on disk; creates destination folders, destination must not exist."""
    return _call("move_file", {"src": src, "dst": dst})


@mcp.tool()
def read_file(path: str, encoding: str | None = None, max_bytes: int | None = None) -> dict:
    """Read a text file from disk and return its content (text only, default UTF-8, max 200 KB)."""
    args: dict[str, Any] = {"path": path}
    if encoding:
        args["encoding"] = encoding
    if max_bytes is not None:
        args["max_bytes"] = max_bytes
    return _call("read_file", args)


@mcp.tool()
def file_info(path: str) -> dict:
    """Get metadata about a file or folder: kind, size, extension and last modification time."""
    return _call("file_info", {"path": path})


@mcp.tool()
def copy_file(src: str, dst: str) -> dict:
    """Copy a file or folder on disk; creates destination folders, destination must not exist."""
    return _call("copy_file", {"src": src, "dst": dst})


@mcp.tool()
def delete_file(path: str, recursive: bool = False) -> dict:
    """Delete a file from disk (folders require recursive=true). This cannot be undone."""
    return _call("delete_file", {"path": path, "recursive": recursive})


@mcp.tool()
def download_file(url: str, path: str) -> dict:
    """Download a http(s) URL to a local file (max 100 MB); destination must not exist."""
    return _call("download_file", {"url": url, "path": path})


# ------------------------------------------------------------------ view & selection

@mcp.tool()
def set_extent(extent: list[float], crs: str | None = None) -> dict:
    """Zoom the map canvas to an extent [xmin, ymin, xmax, ymax], optionally in another CRS (e.g. 'EPSG:4326')."""
    args: dict[str, Any] = {"extent": extent}
    if crs:
        args["crs"] = crs
    return _call("set_extent", args)


@mcp.tool()
def clear_selection(layer: str | None = None) -> dict:
    """Clear the feature selection on a layer, or on all layers if layer is omitted."""
    return _call("clear_selection", {"layer": layer} if layer else {})


@mcp.tool()
def zoom_to_selection(layer: str) -> dict:
    """Zoom the map canvas to the selected features of a layer."""
    return _call("zoom_to_selection", {"layer": layer})


@mcp.tool()
def select_by_location(
    layer: str,
    other: str,
    predicate: str = "intersects",
    method: str = "new",
) -> dict:
    """Select features of 'layer' by spatial relationship with 'other'. predicate: intersects, contains, disjoint, equals, touches, overlaps, within, crosses. method: new, add, within, remove."""
    return _call("select_by_location", {
        "layer": layer,
        "other": other,
        "predicate": predicate,
        "method": method,
    })


@mcp.tool()
def zoom_to_project() -> dict:
    """Pan and zoom the map canvas so all layers of the project fit in view."""
    return _call("zoom_to_project", {})


# ------------------------------------------------------------------ processing

@mcp.tool()
def search_algorithms(query: str = "", limit: int = 15) -> list[dict]:
    """Search Processing algorithms by keyword; returns ids for run_algorithm."""
    return _call("search_algorithms", {"query": query, "limit": limit})["algorithms"]


@mcp.tool()
def get_algorithm_info(id: str) -> dict:
    """Describe a Processing algorithm: parameter names, types, defaults, enum options, outputs and help text. Use before run_algorithm to fill params correctly."""
    return _call("get_algorithm_info", {"id": id})


@mcp.tool()
def run_algorithm(id: str, params: dict) -> dict:
    """Run a Processing algorithm synchronously (blocks until finished, up to 15 min). Layer params accept layer ids or names."""
    return _call("run_algorithm", {"id": id, "params": params}, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def buffer(
    layer: str,
    distance: float,
    segments: int = 5,
    dissolve: bool = False,
    name: str | None = None,
    group: str | None = None,
) -> dict:
    """Buffer vector features by a distance (in layer CRS units) and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer, "distance": distance, "segments": segments, "dissolve": dissolve}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("buffer", args, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def reproject_layer(layer: str, crs: str, name: str | None = None, group: str | None = None) -> dict:
    """Reproject a vector layer to another CRS (e.g. 'EPSG:3857') and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer, "crs": crs}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("reproject_layer", args, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def clip(layer: str, overlay: str, name: str | None = None, group: str | None = None) -> dict:
    """Cut a vector layer with a polygon overlay layer (keep only what falls inside) and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer, "overlay": overlay}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("clip", args, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def intersection(layer: str, overlay: str, name: str | None = None, group: str | None = None) -> dict:
    """Intersect two vector layers (parts of 'layer' overlapping 'overlay', attributes of both) and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer, "overlay": overlay}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("intersection", args, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def dissolve(layer: str, field: str | None = None, name: str | None = None, group: str | None = None) -> dict:
    """Merge features of a vector layer into one (optionally one result per value of a field) and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer}
    if field:
        args["field"] = field
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("dissolve", args, timeout=ALGORITHM_TIMEOUT)


@mcp.tool()
def fix_geometries(layer: str, name: str | None = None, group: str | None = None) -> dict:
    """Repair invalid geometries of a vector layer and add the result as a new layer."""
    args: dict[str, Any] = {"layer": layer}
    if name:
        args["name"] = name
    if group:
        args["group"] = group
    return _call("fix_geometries", args, timeout=ALGORITHM_TIMEOUT)


# ------------------------------------------------------------------ rendering & layouts

@mcp.tool()
def render_map(
    output_path: str,
    width: int = 1600,
    height: int = 1000,
    extent: list[float] | None = None,
    crs: str | None = None,
) -> dict:
    """Render the current map view to png/jpg without altering the user's view."""
    args: dict[str, Any] = {"output_path": output_path, "width": width, "height": height}
    if extent:
        args["extent"] = extent
    if crs:
        args["crs"] = crs
    return _call("render_map", args)


@mcp.tool()
def list_layouts() -> list[dict]:
    """List print layouts (composer pages) in the project with page sizes in mm."""
    return _call("list_layouts", {})["layouts"]


@mcp.tool()
def create_layout(name: str, width_mm: int = 210, height_mm: int = 297) -> dict:
    """Create a print layout with a page and a map item showing the current view; export it later with export_layout."""
    return _call("create_layout", {"name": name, "width_mm": width_mm, "height_mm": height_mm})


@mcp.tool()
def export_layout(name: str, output_path: str, dpi: int = 300) -> dict:
    """Export a print layout to PDF or image (.pdf, .png, .jpg)."""
    return _call("export_layout", {"name": name, "output_path": output_path, "dpi": dpi})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
