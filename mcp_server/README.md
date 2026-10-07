# copla

**MCP server that connects AI assistants to a live QGIS session** through the
[Copla] plugin. Typed tools only — **no arbitrary code execution**.

```
AI client (Claude / opencode / Cursor / VS Code / Gemini CLI)
        │  MCP (stdio)
        ▼
copla  (this package)
        │  HTTP + token, 127.0.0.1
        ▼
Copla plugin  (inside QGIS Desktop)
        │
        ▼
PyQGIS: layers, features, Processing, layouts, rendering
```

## Install

1. In QGIS: *Plugins → Manage and Install Plugins → Installed → **Copla***
   (or install from ZIP). The **Copla** dock shows **● Activo** and a
   ready-to-paste config snippet for your AI client.
2. MCP side (no configuration needed — it auto-discovers QGIS).
   Requires [uv](https://docs.astral.sh/uv/). Not on PyPI yet, install
   straight from the repo:

```bash
pip install "git+https://github.com/pepehalfonso/copla-plugin#subdirectory=mcp_server"
# or run ad-hoc with:
uvx --from "git+https://github.com/pepehalfonso/copla-plugin#subdirectory=mcp_server" copla
```

3. Paste the snippet from the dock into your client. Example for **opencode**:

```json
{
  "mcp": {
    "copla": {
      "type": "local",
      "enabled": true,
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/pepehalfonso/copla-plugin#subdirectory=mcp_server",
        "copla"
      ]
    }
  }
}
```

## Tools (63)

| Group | Tools |
|---|---|
| Health | `diagnose`, `list_qgis_tools` |
| Project | `get_project_info`, `load_project`, `save_project`, `set_project_crs` |
| Layers | `list_layers`, `get_layer_info`, `add_layer`, `create_layer`, `download_layer`, `http_get`, `http_post`, `save_layer_as`, `add_basemap`, `remove_layer`, `remove_group`, `rename_layer`, `set_layer_visibility`, `zoom_to_layer`, `create_group`, `rename_group`, `move_layer` |
| Features | `get_features`, `select_features`, `run_expression`, `unique_values` |
| Editing | `add_features`, `update_attributes`, `delete_features`, `add_field`, `remove_field`, `rename_field`, `calculate_field` |
| Style | `set_renderer`, `set_labels`, `save_style`, `load_style`, `copy_style` |
| View & selection | `set_extent`, `clear_selection`, `zoom_to_selection`, `select_by_location`, `zoom_to_project` |
| Files | `list_directory`, `move_file`, `read_file`, `file_info`, `copy_file`, `delete_file`, `download_file` |
| Processing | `search_algorithms`, `get_algorithm_info`, `run_algorithm`, `buffer`, `reproject_layer`, `clip`, `intersection`, `dissolve`, `fix_geometries` |
| Output | `render_map`, `list_layouts`, `export_layout`, `create_layout` |

There is deliberately **no `execute_python` tool**. Every operation is a
validated, reviewed function inside the plugin.

## Security

- Server binds to `127.0.0.1` only.
- Random token per profile (`copla.token`), required on every call.
- Requests with a browser `Origin` header are rejected (no CSRF from web pages).
- Token/port are shared with the MCP server through `copla.json` inside
  your QGIS profile — never over the network.

## Environment overrides

| Variable | Meaning |
|---|---|
| `COPLA_PORT` + `COPLA_TOKEN` | Connect directly, skip discovery |
| `COPLA_CONFIG` | Path to a specific `copla.json` |
| `COPLA_PROFILE` | QGIS profile directory to search |

## Development

```bash
pip install -e ./mcp_server
python -m copla.server
```

License: GPL-2.0-or-later.

[Copla]: https://github.com/pepehalfonso/copla-plugin
