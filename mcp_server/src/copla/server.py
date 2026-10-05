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
def remove_layer(layer: str) -> dict:
    """Remove a layer from the project (the file on disk is kept)."""
    return _call("remove_layer", {"layer": layer})


@mcp.tool()
def rename_layer(layer: str, name: str) -> dict:
    """Rename a layer."""
    return _call("rename_layer", {"layer": layer, "name": name})


@mcp.tool()
def set_layer_visibility(layer: str, visible: bool) -> dict:
    """Show or hide a layer on the map."""
    return _call("set_layer_visibility", {"layer": layer, "visible": visible})


@mcp.tool()
def zoom_to_layer(layer: str) -> dict:
    """Pan and zoom the QGIS map canvas to a layer's extent."""
    return _call("zoom_to_layer", {"layer": layer})


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


# ------------------------------------------------------------------ processing

@mcp.tool()
def search_algorithms(query: str = "", limit: int = 15) -> list[dict]:
    """Search Processing algorithms by keyword; returns ids for run_algorithm."""
    return _call("search_algorithms", {"query": query, "limit": limit})["algorithms"]


@mcp.tool()
def run_algorithm(id: str, params: dict) -> dict:
    """Run a Processing algorithm synchronously (blocks until finished, up to 15 min). Layer params accept layer ids or names."""
    return _call("run_algorithm", {"id": id, "params": params}, timeout=ALGORITHM_TIMEOUT)


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
def export_layout(name: str, output_path: str, dpi: int = 300) -> dict:
    """Export a print layout to PDF or image (.pdf, .png, .jpg)."""
    return _call("export_layout", {"name": name, "output_path": output_path, "dpi": dpi})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
