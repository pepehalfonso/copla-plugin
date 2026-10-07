"""End-to-end test: real MCP client over stdio -> copla -> live QGIS.

Run with QGIS open and the Copla plugin active:
    python tests/test_e2e_stdio.py
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    joined = "\n".join(parts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "copla.server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("MCP tools (%d): %s" % (len(names), ", ".join(names)))
            expected = {
                "diagnose", "list_qgis_tools", "get_project_info", "load_project",
                "save_project", "set_project_crs", "list_layers", "get_layer_info",
                "add_layer", "create_layer", "download_layer", "http_get",
                "save_layer_as", "add_basemap", "remove_layer", "remove_group",
                "rename_layer",
                "set_layer_visibility", "zoom_to_layer", "get_features",
                "select_features", "run_expression", "add_features",
                "update_attributes", "delete_features", "set_renderer",
                "set_labels", "set_extent", "clear_selection",
                "zoom_to_selection", "list_directory", "move_file",
                "search_algorithms", "get_algorithm_info",
                "run_algorithm", "render_map", "list_layouts", "export_layout",
                "create_group", "rename_group", "move_layer",
                "add_field", "remove_field", "rename_field", "calculate_field",
                "unique_values", "select_by_location", "zoom_to_project",
                "save_style", "load_style", "copy_style",
                "read_file", "file_info", "copy_file", "delete_file",
                "download_file", "http_post",
                "buffer", "reproject_layer", "clip", "intersection",
                "dissolve", "fix_geometries", "create_layout",
            }
            missing = expected - set(names)
            assert not missing, "missing tools: %s" % missing

            r = await session.call_tool("diagnose", {})
            data = payload(r)
            print("diagnose ->", json.dumps(data, ensure_ascii=False))
            assert not r.is_error, "diagnose failed"
            assert data["ok"] is True

            r = await session.call_tool("list_layers", {})
            print("list_layers ->", payload(r))
            assert not r.is_error

            r = await session.call_tool("get_project_info", {})
            print("get_project_info ->", json.dumps(payload(r), ensure_ascii=False))
            assert not r.is_error

            r = await session.call_tool("search_algorithms", {"query": "clip", "limit": 3})
            algs = payload(r)
            print("search_algorithms ->", json.dumps(algs, ensure_ascii=False)[:300])
            assert not r.is_error and algs

            r = await session.call_tool("get_algorithm_info", {"id": "native:buffer"})
            info = payload(r)
            print("get_algorithm_info ->", json.dumps(info, ensure_ascii=False)[:300])
            assert not r.is_error
            pnames = [p["name"] for p in info["parameters"]]
            assert "DISTANCE" in pnames, "buffer should expose DISTANCE"

            r = await session.call_tool("clear_selection", {})
            print("clear_selection ->", payload(r))
            assert not r.is_error

            r = await session.call_tool("get_layer_info", {"layer": "no-existe-xyz"})
            err_text = str(payload(r))
            print("error-case is_error=%s -> %s" % (r.is_error, err_text[:160]))
            assert r.is_error, "expected an error for unknown layer"
            assert "no-existe-xyz" in err_text, "error message should name the layer"

            print("ALL E2E TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
