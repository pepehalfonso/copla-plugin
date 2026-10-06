"""Copla - main plugin: HTTP server lifecycle + dock UI.

The dock is the "1-click" part of the experience: it shows a ready-to-
paste MCP config snippet for each popular AI client, so users never
have to hand-write configuration.
"""

import html
import json
import os
import secrets

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtGui import QFontDatabase, QGuiApplication, QIcon

from .chat import AGENTS, DEFAULT_SYSTEM_PROMPT, PRESETS, ChatEngine, save_config
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
        self.chat = None
        self._stream_open = False
        self._cfg_loading = False
        self._chat_was_at_end = True

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
        if self.chat is not None:
            self.chat.stop()
            self.chat = None
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
        self.chat = ChatEngine(self.iface.mainWindow())
        self.chat.token_received.connect(self._on_chat_token)
        self.chat.tool_event.connect(self._on_chat_tool_event)
        self.chat.assistant_finished.connect(self._on_chat_finished)
        self.chat.error_raised.connect(self._on_chat_error)
        self.chat.busy_changed.connect(self._on_chat_busy)

        dock = QDockWidget("Copla", self.iface.mainWindow())
        dock.setObjectName("CoplaDock")
        conn_widget = QWidget()
        layout = QVBoxLayout(conn_widget)
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

        tabs = QTabWidget()
        tabs.addTab(conn_widget, "Conexión")
        tabs.addTab(self._build_chat_tab(), "Chat")
        dock.setWidget(tabs)
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

    # ------------------------------------------------------------- chat tab

    def _build_chat_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self.cfg_toggle = QPushButton("Configuración ▸")
        self.cfg_toggle.setCheckable(True)
        self.cfg_toggle.setStyleSheet("text-align:left; padding:2px;")
        self.cfg_toggle.toggled.connect(self._chat_toggle_cfg)
        outer.addWidget(self.cfg_toggle)

        self.cfg_group = QGroupBox("Proveedor del chat")
        cfg = QGridLayout(self.cfg_group)
        cfg.setSpacing(6)
        cfg.addWidget(QLabel("Proveedor:"), 0, 0)
        self.cfg_preset = QComboBox()
        for label in PRESETS:
            self.cfg_preset.addItem(label)
        self.cfg_preset.currentTextChanged.connect(self._chat_preset_changed)
        cfg.addWidget(self.cfg_preset, 0, 1, 1, 3)
        cfg.addWidget(QLabel("Modelo:"), 1, 0)
        self.cfg_model = QLineEdit()
        cfg.addWidget(self.cfg_model, 1, 1, 1, 3)
        cfg.addWidget(QLabel("URL base:"), 2, 0)
        self.cfg_url = QLineEdit()
        self.cfg_url.setPlaceholderText("https://api.openai.com/v1")
        cfg.addWidget(self.cfg_url, 2, 1, 1, 3)
        cfg.addWidget(QLabel("API key:"), 3, 0)
        self.cfg_key = QLineEdit()
        self.cfg_key.setEchoMode(QLineEdit.Password)
        cfg.addWidget(self.cfg_key, 3, 1)
        show_key = QCheckBox("Mostrar")
        show_key.toggled.connect(
            lambda on: self.cfg_key.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        cfg.addWidget(show_key, 3, 2)
        save_btn = QPushButton("Guardar")
        save_btn.clicked.connect(self._chat_save_config)
        cfg.addWidget(save_btn, 3, 3)
        self.cfg_hint = QLabel()
        self.cfg_hint.setWordWrap(True)
        self.cfg_hint.setStyleSheet("color:#5f6368; font-size:11px;")
        cfg.addWidget(self.cfg_hint, 4, 1, 1, 3)
        cfg.addWidget(QLabel("Prompt de sistema (agente Copla):"), 5, 0)
        self.cfg_prompt = QTextEdit()
        self.cfg_prompt.setPlainText(
            self.chat.config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        )
        self.cfg_prompt.setFixedHeight(70)
        cfg.addWidget(self.cfg_prompt, 5, 1, 1, 3)
        outer.addWidget(self.cfg_group)

        self.cfg_url.setText(self.chat.config.get("base_url", ""))
        self.cfg_model.setText(self.chat.config.get("model", ""))
        self.cfg_key.setText(self.chat.config.get("api_key", ""))
        self._chat_match_preset()
        configured = bool(self.chat.config.get("model"))
        self.cfg_group.setVisible(not configured)
        self.cfg_toggle.setChecked(not configured)

        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setMinimumHeight(150)
        outer.addWidget(self.chat_view, 1)

        agent_row = QHBoxLayout()
        agent_row.setSpacing(6)
        agent_row.addWidget(QLabel("Agente:"))
        self.chat_agent = QComboBox()
        for agent_name in AGENTS:
            self.chat_agent.addItem(agent_name)
        self.chat_agent.setMinimumWidth(160)
        saved_agent = self.chat.config.get("agent") or "Copla"
        saved_idx = self.chat_agent.findText(saved_agent)
        self.chat_agent.setCurrentIndex(saved_idx if saved_idx >= 0 else 0)
        agent_row.addWidget(self.chat_agent)
        self.agent_desc = QLabel(
            AGENTS[self.chat_agent.currentText()]["description"]
        )
        self.agent_desc.setStyleSheet("color:#5f6368; font-size:11px;")
        self.agent_desc.setWordWrap(True)
        agent_row.addWidget(self.agent_desc, 1)
        self.chat_agent.currentTextChanged.connect(self._chat_agent_changed)
        outer.addLayout(agent_row)

        self.chat_input = _ChatInput()
        self.chat_input.setPlaceholderText(
            "Escribí tu pedido… (Enter envía, Shift+Enter nueva línea)"
        )
        self.chat_input.setFixedHeight(54)
        self.chat_input.submitted.connect(self._chat_send)
        outer.addWidget(self.chat_input)

        row = QHBoxLayout()
        self.chat_send_btn = QPushButton("Enviar")
        self.chat_send_btn.clicked.connect(self._chat_send)
        self.chat_stop_btn = QPushButton("Detener")
        self.chat_stop_btn.clicked.connect(self._chat_stop)
        self.chat_stop_btn.setEnabled(False)
        new_btn = QPushButton("Nueva conversación")
        new_btn.clicked.connect(self._chat_new)
        self.chat_status = QLabel("Listo")
        self.chat_status.setStyleSheet("color:#5f6368; font-size:11px;")
        row.addWidget(self.chat_send_btn)
        row.addWidget(self.chat_stop_btn)
        row.addWidget(new_btn)
        row.addStretch(1)
        row.addWidget(self.chat_status)
        outer.addLayout(row)

        self._stream_open = False
        self._render_chat_history()
        return tab

    def _chat_send(self):
        if self.chat is None:
            return
        text = self.chat_input.toPlainText().strip()
        if not text or self.chat.busy:
            return
        if not self.chat.config.get("model") or not self.chat.config.get(
            "base_url"
        ):
            self.cfg_group.setVisible(True)
            self.cfg_toggle.setChecked(True)
            self.chat_status.setText("Configurá proveedor y modelo")
            return
        if not self.chat.messages:
            self.chat_view.clear()
        self.chat_input.clear()
        self._chat_close_stream()
        self._chat_break(double=True)
        self._chat_append_html(
            "<span style='color:#1a7f37; font-size:12px'><b>Vos:</b> "
        )
        self._chat_append_plain(text)
        self._chat_append_html("</span>")
        self.chat.send(text)

    def _chat_stop(self):
        if self.chat is not None:
            self.chat.stop()
            self.chat_status.setText("Detenido")

    def _chat_agent_changed(self, name):
        if self.chat is None or name not in AGENTS:
            return
        self.chat.config["agent"] = name
        save_config(self.chat.config)
        self.agent_desc.setText(AGENTS[name]["description"])
        self.chat_status.setText("Agente: %s" % name)

    def _chat_new(self):
        if self.chat is None or self.chat.busy:
            return
        self.chat.new_conversation()
        self._render_chat_history()
        self.chat_status.setText("Listo")

    def _chat_toggle_cfg(self, checked):
        self.cfg_group.setVisible(checked)
        self.cfg_toggle.setText(
            "Configuración ▾" if checked else "Configuración ▸"
        )

    def _chat_preset_changed(self, label):
        if self._cfg_loading:
            return
        preset = PRESETS.get(label)
        self.cfg_hint.setText(preset.get("hint", "") if preset else "")
        if preset and preset.get("base_url"):
            self.cfg_url.setText(preset["base_url"])
            if preset.get("model"):
                self.cfg_model.setText(preset["model"])

    def _chat_match_preset(self):
        self._cfg_loading = True
        try:
            matched = "Personalizado"
            url = (self.chat.config.get("base_url") or "").rstrip("/")
            model = self.chat.config.get("model") or ""
            for label, preset in PRESETS.items():
                if (
                    preset.get("base_url")
                    and preset["base_url"].rstrip("/") == url
                    and (not model or preset.get("model") == model)
                ):
                    matched = label
                    break
            idx = self.cfg_preset.findText(matched)
            if idx >= 0:
                self.cfg_preset.setCurrentIndex(idx)
            preset = PRESETS.get(matched)
            self.cfg_hint.setText(preset.get("hint", "") if preset else "")
        finally:
            self._cfg_loading = False

    def _chat_save_config(self):
        if self.chat is None:
            return
        self.chat.config.update(
            {
                "base_url": self.cfg_url.text().strip(),
                "api_key": self.cfg_key.text().strip(),
                "model": self.cfg_model.text().strip(),
                "system_prompt": self.cfg_prompt.toPlainText().strip()
                or DEFAULT_SYSTEM_PROMPT,
            }
        )
        save_config(self.chat.config)
        self.chat_status.setText("Configuración guardada")

    def _on_chat_token(self, text):
        if not self._stream_open:
            self._chat_break(double=True)
            self._chat_append_html(
                "<span style='font-size:12px'><b>Copla:</b> "
            )
            self._stream_open = True
        self._chat_append_plain(text)

    def _on_chat_tool_event(self, name, summary, status):
        self._chat_close_stream()
        color = "#5f6368" if status == "ok" else "#b42318"
        label = "ok" if status == "ok" else "error"
        self._chat_break()
        self._chat_append_html(
            "<span style='color:%s; font-family:Consolas,monospace; "
            "font-size:11px'>&#9656; %s — %s</span>"
            % (color, html.escape(str(summary)), label)
        )
        self.chat_status.setText("Ejecutando %s…" % name)

    def _on_chat_finished(self, text):
        self._chat_close_stream()
        self.chat_status.setText("Listo")

    def _on_chat_error(self, message):
        self._chat_close_stream()
        self._chat_break(double=True)
        self._chat_append_html(
            "<span style='color:#b42318; font-size:12px'>"
            "<b>Error:</b> %s</span>" % html.escape(str(message))
        )
        self.chat_status.setText("Error")

    def _on_chat_busy(self, busy):
        self.chat_send_btn.setEnabled(not busy)
        self.chat_stop_btn.setEnabled(busy)
        if busy:
            self.chat_status.setText("Pensando…")

    def _chat_at_end(self):
        bar = self.chat_view.verticalScrollBar()
        return bar.value() >= bar.maximum() - 24

    def _chat_scroll(self, force=False):
        if force or self._chat_was_at_end:
            bar = self.chat_view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _chat_append_html(self, text):
        self._chat_was_at_end = self._chat_at_end()
        cursor = self.chat_view.textCursor()
        cursor.movePosition(cursor.End)
        self.chat_view.setTextCursor(cursor)
        self.chat_view.insertHtml(text)
        self._chat_scroll()

    def _chat_append_plain(self, text):
        self._chat_was_at_end = self._chat_at_end()
        cursor = self.chat_view.textCursor()
        cursor.movePosition(cursor.End)
        self.chat_view.setTextCursor(cursor)
        self.chat_view.insertPlainText(text)
        self._chat_scroll()

    def _chat_close_stream(self):
        if self._stream_open:
            self._stream_open = False
            self._chat_append_html("</span>")

    def _chat_break(self, double=False):
        if self.chat_view.document().characterCount() > 1:
            self._chat_append_html("<br>" + ("<br>" if double else ""))

    def _render_chat_history(self):
        self._stream_open = False
        messages = self.chat.messages if self.chat is not None else []
        if not messages:
            self.chat_view.setHtml(
                "<span style='color:#666; font-size:12px'>Configurá el "
                "proveedor (si hace falta) y escribí para empezar. La IA usa "
                "las herramientas de Copla para trabajar con capas, estilos "
                "y archivos de QGIS.</span>"
            )
            return
        self.chat_view.clear()
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if not content:
                continue
            self._chat_break(double=True)
            if role == "user":
                self._chat_append_html(
                    "<span style='color:#1a7f37; font-size:12px'><b>Vos:</b> "
                )
            else:
                self._chat_append_html(
                    "<span style='font-size:12px'><b>Copla:</b> "
                )
            self._chat_append_plain(str(content))
            self._chat_append_html("</span>")
        self._chat_scroll(force=True)


class _ChatInput(QTextEdit):
    submitted = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)
