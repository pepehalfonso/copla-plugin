"""Copla - main plugin: HTTP server lifecycle + dock UI.

The dock is the "1-click" part of the experience: it shows a ready-to-
paste MCP config snippet for each popular AI client, so users never
have to hand-write configuration.
"""

import json
import os
import secrets

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtGui import QFontDatabase, QGuiApplication, QIcon

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
        self._qnam = None
        self._tabify_attempted = False

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
        if self.dock is not None:
            self.test_result.setText("")
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
        if self.dock is not None:
            self.test_result.setText("")
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
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        try:
            mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        except AttributeError:
            mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        group_state = QGroupBox("Estado")
        state_l = QVBoxLayout(group_state)
        state_l.setSpacing(6)
        self.status_label = QLabel()
        self.info_label = QLabel(
            "Copla v%s · QGIS %s · %d herramientas"
            % (PLUGIN_VERSION, Qgis.QGIS_VERSION, len(tools_manifest()))
        )
        self.info_label.setStyleSheet("color:#5f6368; font-size:11px;")
        row_state = QHBoxLayout()
        self.toggle_btn = QPushButton("Detener")
        self.toggle_btn.clicked.connect(self._toggle_from_dock)
        self.test_btn = QPushButton("Probar conexión")
        self.test_btn.clicked.connect(self._self_test)
        row_state.addWidget(self.toggle_btn)
        row_state.addWidget(self.test_btn)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setStyleSheet("font-size:11px;")
        state_l.addWidget(self.status_label)
        state_l.addWidget(self.info_label)
        state_l.addLayout(row_state)
        state_l.addWidget(self.test_result)
        layout.addWidget(group_state)

        group_token = QGroupBox("Token")
        token_l = QVBoxLayout(group_token)
        token_l.setSpacing(6)
        self.token_label = QLabel()
        self.token_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.token_label.setFont(mono)
        row_token = QHBoxLayout()
        self.show_token = QCheckBox("Mostrar")
        self.show_token.toggled.connect(lambda _: self._refresh_status())
        copy_token = QPushButton("Copiar token")
        copy_token.clicked.connect(self._copy_token)
        row_token.addWidget(self.show_token)
        row_token.addWidget(copy_token)
        row_token.addStretch(1)
        token_l.addWidget(self.token_label)
        token_l.addLayout(row_token)
        layout.addWidget(group_token)

        group_client = QGroupBox("Conectar cliente IA")
        client_l = QVBoxLayout(group_client)
        client_l.setSpacing(6)
        client_l.addWidget(QLabel("Cliente:"))
        self.client_combo = QComboBox()
        for key, entry in SNIPPETS.items():
            self.client_combo.addItem(entry["label"], key)
        self.client_combo.currentIndexChanged.connect(lambda _: self._refresh_snippet())
        client_l.addWidget(self.client_combo)
        self.snippet_hint = QLabel()
        self.snippet_hint.setWordWrap(True)
        self.snippet_hint.setStyleSheet("color:#5f6368; font-size:11px;")
        client_l.addWidget(self.snippet_hint)
        self.snippet_edit = QPlainTextEdit()
        self.snippet_edit.setReadOnly(True)
        self.snippet_edit.setFont(mono)
        self.snippet_edit.setMinimumHeight(110)
        client_l.addWidget(self.snippet_edit)
        copy_button = QPushButton("Copiar configuración")
        copy_button.clicked.connect(self._copy_snippet)
        client_l.addWidget(copy_button)
        layout.addWidget(group_client)

        note = QLabel(
            "Requisitos del lado IA: uv (docs.astral.sh/uv).\n"
            "Los snippets usan la forma Git: listos para pegar.\n"
            "El servidor MCP detecta QGIS automáticamente."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(note)

        dock.setWidget(widget)
        try:
            area = Qt.RightDockWidgetArea
        except AttributeError:
            area = Qt.DockWidgetArea.RightDockWidgetArea
        self.iface.addDockWidget(area, dock)
        self.dock = dock
        self._tabify_retries = 0
        self._try_tabify()
        QTimer.singleShot(3000, self._tabify_recheck)
        self._refresh_snippet()
        self._refresh_status()

    def _tabify_recheck(self):
        if self.dock is None or self._tabify_attempted:
            return
        self._tabify_retries += 1
        self._try_tabify()
        if not self._tabify_attempted and self._tabify_retries < 20:
            QTimer.singleShot(3000, self._tabify_recheck)

    def _try_tabify(self):
        if self.dock is None:
            return
        window = self.iface.mainWindow()
        if window.tabifiedDockWidgets(self.dock):
            self.dock.raise_()
            self._tabify_attempted = True
            return
        if self._tabify_attempted:
            return
        try:
            area = Qt.RightDockWidgetArea
        except AttributeError:
            area = Qt.DockWidgetArea.RightDockWidgetArea
        for other in window.findChildren(QDockWidget):
            if (
                other is not self.dock
                and other.isVisible()
                and window.dockWidgetArea(other) == area
            ):
                window.tabifyDockWidget(other, self.dock)
                self.dock.raise_()
                self._tabify_attempted = True
                return

    def _refresh_status(self):
        if self.dock is None:
            return
        running = self.server is not None and self.server.is_listening()
        if running:
            self.status_label.setText(
                "<b style='color:#1a7f37'>● Activo</b> — 127.0.0.1:%d" % self.port
            )
            self.toggle_btn.setText("Detener")
            self.test_btn.setEnabled(True)
        else:
            self.status_label.setText("<b style='color:#b42318'>● Detenido</b>")
            self.toggle_btn.setText("Iniciar")
            self.test_btn.setEnabled(False)
        token = self.token or "-"
        if self.show_token.isChecked():
            self.token_label.setText(token)
        else:
            self.token_label.setText("%s..." % token[:8])

    def _toggle_from_dock(self):
        running = self.server is not None and self.server.is_listening()
        if running:
            self.stop()
        else:
            self.start()

    def _copy_token(self):
        QGuiApplication.clipboard().setText(self.token or "")
        self._bar("Token copiado al portapapeles")

    def _self_test(self):
        if self.server is None or not self.server.is_listening():
            return
        self.test_btn.setEnabled(False)
        self.test_result.setStyleSheet("font-size:11px; color:#5f6368;")
        self.test_result.setText("Probando…")
        request = QNetworkRequest(
            QUrl("http://127.0.0.1:%d/v1/tools" % self.port)
        )
        request.setRawHeader(b"X-Copla-Token", (self.token or "").encode("ascii"))
        try:
            request.setTransferTimeout(4000)
        except AttributeError:
            pass
        if self._qnam is None:
            self._qnam = QNetworkAccessManager(self.dock)
        reply = self._qnam.get(request)
        reply.finished.connect(lambda: self._self_test_done(reply))

    def _self_test_done(self, reply):
        try:
            if self.dock is None:
                return
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            try:
                no_error = QNetworkReply.NetworkError.NoError
            except AttributeError:
                no_error = QNetworkReply.NoError
            if reply.error() == no_error:
                tools = []
                try:
                    data = json.loads(bytes(reply.readAll().data()).decode("utf-8"))
                    tools = data.get("tools") or []
                except (ValueError, UnicodeDecodeError, AttributeError):
                    pass
                self.test_result.setStyleSheet("font-size:11px; color:#1a7f37;")
                self.test_result.setText(
                    "✓ Responde correctamente — %d herramientas" % len(tools)
                )
            elif status == 401:
                self.test_result.setStyleSheet("font-size:11px; color:#b42318;")
                self.test_result.setText("✗ Token rechazado (401)")
            else:
                self.test_result.setStyleSheet("font-size:11px; color:#b42318;")
                self.test_result.setText("✗ Sin respuesta: %s" % reply.errorString())
        finally:
            reply.deleteLater()
            if self.dock is not None:
                self.test_btn.setEnabled(True)

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
