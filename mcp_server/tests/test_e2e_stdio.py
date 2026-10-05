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
                "save_project", "list_layers", "get_layer_info", "add_layer",
                "remove_layer", "rename_layer", "set_layer_visibility",
                "zoom_to_layer", "get_features", "select_features",
                "run_expression", "search_algorithms", "run_algorithm",
                "render_map", "list_layouts", "export_layout",
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

            r = await session.call_tool("get_layer_info", {"layer": "no-existe-xyz"})
            err_text = str(payload(r))
            print("error-case is_error=%s -> %s" % (r.is_error, err_text[:160]))
            assert r.is_error, "expected an error for unknown layer"
            assert "no-existe-xyz" in err_text, "error message should name the layer"

            print("ALL E2E TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
