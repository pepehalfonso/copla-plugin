"""Find a running Copla instance via the profile discovery file."""

import json
import os
import sys

CONFIG_NAME = "copla.json"


def _profile_roots():
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


def find_config():
    """Return {"port", "token", ...} of the most recently written config, or None."""
    best = None
    best_mtime = -1.0
    for root in _profile_roots():
        candidates = []
        if os.path.isfile(root) and root.endswith(CONFIG_NAME):
            candidates.append(root)
        elif os.path.isdir(root):
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in entries:
                candidate = os.path.join(root, entry, CONFIG_NAME)
                if os.path.isfile(candidate):
                    candidates.append(candidate)
                nested = os.path.join(root, entry, "profiles")
                if os.path.isdir(nested):
                    try:
                        for profile in os.listdir(nested):
                            candidate = os.path.join(nested, profile, CONFIG_NAME)
                            if os.path.isfile(candidate):
                                candidates.append(candidate)
                    except OSError:
                        pass
        for path in candidates:
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
