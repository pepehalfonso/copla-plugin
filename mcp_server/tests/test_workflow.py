"""Workflow test through MCP: add a real shapefile, inspect, render, clean up.

Run with QGIS open and the Copla plugin active.
"""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SHAPEFILE = r"C:\Users\Daniel Malan\Desktop\GIS_QGIS\area\CerroLargo.shp"


def payload(result):
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    parts = [getattr(b, "text", "") for b in result.content]
    joined = "\n".join(p for p in parts if p)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


async def main():
    out_png = os.path.join(tempfile.gettempdir(), "copla_workflow_test.png")
    params = StdioServerParameters(command=sys.executable, args=["-m", "copla.server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            r = await session.call_tool("add_layer", {"path": SHAPEFILE, "name": "CerroLargo_test"})
            layer = payload(r)
            print("add_layer ->", json.dumps(layer, ensure_ascii=False)[:300])
            assert not r.is_error, "add_layer failed: %s" % layer
            layer_id = None
            if isinstance(layer, dict):
                layer_id = layer.get("id") or (layer.get("result") or {}).get("id")
            assert layer_id, "no layer id in %r" % layer
            print("layer id:", layer_id)

            try:
                r = await session.call_tool("get_layer_info", {"layer": layer_id})
                info = payload(r)
                print("get_layer_info ->", json.dumps(info, ensure_ascii=False)[:400])
                assert not r.is_error
                if isinstance(info, dict) and "result" in info and "fields" not in info:
                    info = info["result"]
                assert info.get("fields"), info

                r = await session.call_tool("get_features", {"layer": layer_id, "limit": 3})
                feats = payload(r)
                print("get_features ->", json.dumps(feats, ensure_ascii=False)[:400])
                assert not r.is_error

                r = await session.call_tool("run_expression", {"layer": layer_id, "expression": "1", "aggregate": {"function": "count", "expression": "$id"}})
                agg = payload(r)
                print("aggregate ->", json.dumps(agg, ensure_ascii=False)[:200])
                assert not r.is_error

                r = await session.call_tool("render_map", {"output_path": out_png, "width": 800, "height": 600})
                rendered = payload(r)
                print("render_map ->", json.dumps(rendered, ensure_ascii=False)[:300])
                assert not r.is_error, "render failed: %s" % rendered
                saved = None
                if isinstance(rendered, dict):
                    saved = rendered.get("saved") or (rendered.get("result") or {}).get("saved")
                assert saved and os.path.isfile(saved), "image not written: %r" % rendered
                print("image size:", os.path.getsize(saved), "bytes")

                print("WORKFLOW TEST PASSED")
            finally:
                r = await session.call_tool("remove_layer", {"layer": layer_id})
                print("remove_layer ->", payload(r))


if __name__ == "__main__":
    asyncio.run(main())
