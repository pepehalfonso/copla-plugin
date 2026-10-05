"""Discovery file helpers: lets the MCP server find a running QGIS.

The plugin writes ``copla.json`` (port + token + versions) into the
active QGIS profile while the server is running, and removes it on
stop. The MCP server looks for that file in standard profile locations
on Windows/macOS/Linux.
"""

import json
import os
import sys
import time

CONFIG_NAME = "copla.json"
TOKEN_NAME = "copla.token"


def profile_dirs():
    env_config = os.environ.get("COPLA_CONFIG")
    if env_config:
        yield os.path.dirname(os.path.abspath(env_config))
    env_profile = os.environ.get("COPLA_PROFILE")
    if env_profile:
        yield os.path.abspath(env_profile)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            yield os.path.join(appdata, "QGIS", "QGIS3", "profiles")
    elif sys.platform == "darwin":
        yield os.path.expanduser("~/Library/Application Support/QGIS/QGIS3/profiles")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        yield os.path.join(xdg, "QGIS", "QGIS3", "profiles")


def iter_config_paths():
    for base in profile_dirs():
        try:
            if os.path.isfile(base):
                yield base
                continue
            if not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                candidate = os.path.join(base, entry, CONFIG_NAME)
                if os.path.isfile(candidate):
                    yield candidate
        except OSError:
            continue


def find_config():
    """Return the most recently written copla.json, or None."""
    best = None
    best_mtime = -1.0
    for path in iter_config_paths():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    if best is None:
        return None
    try:
        with open(best, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data or "token" not in data:
        return None
    data["_path"] = best
    return data


def write_config(directory, port, token, extra=None):
    data = {"port": port, "token": token, "written_at": int(time.time())}
    if extra:
        data.update(extra)
    path = os.path.join(directory, CONFIG_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return path


def remove_config(directory):
    path = os.path.join(directory, CONFIG_NAME)
    try:
        os.remove(path)
        return True
    except OSError:
        return False
