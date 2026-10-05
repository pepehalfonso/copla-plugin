"""Build a installable plugin ZIP for plugins.qgis.org / 'Install from ZIP'.

Usage:  python scripts/package_plugin.py
Output: dist/copla-plugin-<version>.zip  (folder inside: copla/)
"""

import pathlib
import re
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
DIST = ROOT / "dist"
PACKAGE_NAME = "copla"
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def plugin_version():
    text = (PLUGIN / "metadata.txt").read_text(encoding="utf-8")
    match = re.search(r"^version=(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else "0.0.0"


def main():
    version = plugin_version()
    DIST.mkdir(exist_ok=True)
    out = DIST / ("copla-plugin-%s.zip" % version)
    if out.exists():
        out.unlink()
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PLUGIN.rglob("*")):
            if path.is_dir():
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            arcname = pathlib.Path(PACKAGE_NAME) / path.relative_to(PLUGIN)
            zf.write(path, arcname.as_posix())
            count += 1
    print("OK: %s (%d files, %d bytes)" % (out, count, out.stat().st_size))


if __name__ == "__main__":
    main()
