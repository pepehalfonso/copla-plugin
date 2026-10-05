"""Copla - main plugin: HTTP server lifecycle + dock UI.

The dock is the "1-click" part of the experience: it shows a ready-to-
paste MCP config snippet for each popular AI client, so users never
have to hand-write configuration.
"""

import json
import os
import secrets

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtGui import QGuiApplication, QIcon

from .discovery import TOKEN_NAME, remove_config, write_config
from .http_server import CoplaHttpServer
from .tools import PLUGIN_VERSION, run_tool, tools_manifest

DEFAULT_PORT = 8970

GIT_DEP = "git+https://github.com/pepehalfonso/copla-plugin#subdirectory=mcp_server"

SNIPPETS = {
    "opencode": {
        "label": "opencode (opencode.json)",
        "hint": "Pegá este objeto dentro de tu opencode.json",
        "body": {
            "mcp": {
                "copla": {
                    "type": "local",
                    "enabled": True,
                    "command": ["uvx", "--from", GIT_DEP, "copla"],
                }
            }
        },
    },
    "claude_desktop": {
        "label": "Claude Desktop",
        "hint": "claude_desktop_config.json",
        "body": {
            "mcpServers": {
                "copla": {"command": "uvx", "args": ["--from", GIT_DEP, "copla"]}
            }
        },
    },
    "claude_code": {
        "label": "Claude Code (terminal)",
        "hint": "Ejecutá este comando en tu terminal",
        "command": 'claude mcp add copla -- uvx --from "%s" copla' % GIT_DEP,
    },
    "cursor": {
        "label": "Cursor (.cursor/mcp.json)",
        "hint": "mismo formato que Claude Desktop",
        "body": {
            "mcpServers": {
                "copla": {"command": "uvx", "args": ["--from", GIT_DEP, "copla"]}
            }
        },
    },
    "vscode": {
        "label": "VS Code (.vscode/mcp.json)",
        "hint": "VS Code MCP config",
        "body": {
            "servers": {
                "copla": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["--from", GIT_DEP, "copla"],
                }
            }
        },
    },
    "gemini_cli": {
        "label": "Gemini CLI (settings.json)",
        "hint": "Gemini CLI MCP config",
        "body": {
            "mcpServers": {
                "copla": {"command": "uvx", "args": ["--from", GIT_DEP, "copla"]}
            }
        },
    },
}


def _profile_dir():
    return QgsApplication.qgisSettingsDirPath()


def _load_token():
    path = os.path.join(_profile_dir(), TOKEN_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(token)
    except OSError:
        pass
    return token


def _snippet_text(key):
    entry = SNIPPETS[key]
    if "command" in entry:
        return entry["command"]
    return json.dumps(entry["body"], indent=2, ensure_ascii=False)


class CoplaPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.port = DEFAULT_PORT
        self.token = None
        self.server = None
        self.dock = None
        self.action = None

    # ------------------------------------------------------------- lifecycle

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.jpg")
        self.action = QAction(QIcon(icon_path), "Copla", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setChecked(True)
        self.action.setToolTip("Servidor local para asistentes de IA (127.0.0.1:%d)" % self.port)
        self.action.triggered.connect(self._toggle_action)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Copla", self.action)
        self._build_dock()
        self.start()

    def unload(self):
        self.stop()
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("Copla", self.action)
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock = None

    def _toggle_action(self, checked):
        if checked:
            if not self.start():
                self.action.setChecked(False)
        else:
            self.stop()

    # ------------------------------------------------------------- server

    def start(self):
        if self.server is not None and self.server.is_listening():
            return True
        self.token = _load_token()
        server = CoplaHttpServer(
            self.port,
            self.token,
            dispatch=self._dispatch,
            parent=None,
            on_log=lambda msg: None,
        )
        if not server.start():
            self._bar(
                "Copla no pudo abrir 127.0.0.1:%d - ¿otra instancia de QGIS?"
                % self.port,
                level="warning",
            )
            return False
        self.server = server
        try:
            write_config(
                _profile_dir(),
                self.port,
                self.token,
                extra={
                    "plugin_version": PLUGIN_VERSION,
                    "qgis_version": Qgis.QGIS_VERSION,
                },
            )
        except OSError:
            pass
        if self.action is not None:
            self.action.setChecked(True)
        self._refresh_status()
        self._bar("Copla activo en 127.0.0.1:%d" % self.port, level="info")
        return True

    def stop(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
        try:
            remove_config(_profile_dir())
        except OSError:
            pass
        if self.action is not None:
            self.action.setChecked(False)
        self._refresh_status()

    def _dispatch(self, tool_name, args):
        if tool_name == "list_tools":
            return tools_manifest()
        return run_tool(tool_name, args)

    def _bar(self, text, level="info"):
        try:
            bar = self.iface.messageBar()
            if level == "warning":
                bar.pushWarning("Copla", text)
            else:
                bar.pushInfo("Copla", text)
        except Exception:
            pass

    # ------------------------------------------------------------- dock UI

    def _build_dock(self):
        dock = QDockWidget("Copla", self.iface.mainWindow())
        dock.setObjectName("CoplaDock")
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        self.token_label = QLabel()
        self.token_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.token_label)

        self.show_token = QCheckBox("Mostrar token")
        self.show_token.toggled.connect(lambda _: self._refresh_status())
        layout.addWidget(self.show_token)

        layout.addWidget(QLabel("Cliente IA:"))
        self.client_combo = QComboBox()
        for key, entry in SNIPPETS.items():
            self.client_combo.addItem(entry["label"], key)
        self.client_combo.currentIndexChanged.connect(lambda _: self._refresh_snippet())
        layout.addWidget(self.client_combo)

        self.snippet_hint = QLabel()
        self.snippet_hint.setWordWrap(True)
        layout.addWidget(self.snippet_hint)

        self.snippet_edit = QPlainTextEdit()
        self.snippet_edit.setReadOnly(True)
        self.snippet_edit.setMinimumHeight(140)
        layout.addWidget(self.snippet_edit)

        copy_button = QPushButton("Copiar configuración")
        copy_button.clicked.connect(self._copy_snippet)
        layout.addWidget(copy_button)

        note = QLabel(
            "Lado MCP: requiere uv (docs.astral.sh/uv).\n"
            "Los snippets ya usan la forma Git, listos para pegar.\n"
            "El servidor MCP detecta QGIS solo."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(note)

        widget.setLayout(layout)
        dock.setWidget(widget)
        try:
            area = Qt.RightDockWidgetArea
        except AttributeError:
            area = Qt.DockWidgetArea.RightDockWidgetArea
        self.iface.addDockWidget(area, dock)
        self.dock = dock
        self._refresh_snippet()
        self._refresh_status()

    def _refresh_status(self):
        if self.dock is None:
            return
        running = self.server is not None and self.server.is_listening()
        if running:
            self.status_label.setText(
                "<b style='color:#1a7f37'>● Activo</b> en 127.0.0.1:%d" % self.port
            )
        else:
            self.status_label.setText("<b style='color:#b42318'>● Detenido</b>")
        if self.show_token.isChecked():
            self.token_label.setText("Token: %s" % (self.token or "-"))
        else:
            token = self.token or "-"
            self.token_label.setText("Token: %s..." % token[:8])

    def _refresh_snippet(self):
        if self.dock is None:
            return
        key = self.client_combo.currentData()
        entry = SNIPPETS.get(key)
        if entry is None:
            return
        self.snippet_hint.setText(entry["hint"])
        self.snippet_edit.setPlainText(_snippet_text(key))

    def _copy_snippet(self):
        QGuiApplication.clipboard().setText(self.snippet_edit.toPlainText())
        self._bar("Configuración copiada al portapapeles")
