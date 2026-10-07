"""Copla - main plugin: HTTP server lifecycle + dock UI.

The dock is the "1-click" part of the experience: it shows a ready-to-
paste MCP config snippet for each popular AI client, so users never
have to hand-write configuration.
"""

import html
import json
import os
import re
import secrets

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.PyQt.QtGui import (
    QColor,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QTextCharFormat,
    QTextCursor,
    QTextLength,
    QTextTableFormat,
)

from .chat import (
    AGENTS,
    DEFAULT_SYSTEM_PROMPT,
    PRESETS,
    ChatEngine,
    _args_summary,
    _safe_json,
    save_config,
)
from .discovery import TOKEN_NAME, remove_config, write_config
from .http_server import CoplaHttpServer
from .tools import PLUGIN_VERSION, run_tool, tools_manifest

DEFAULT_PORT = 8970

CHAT_SUGGESTIONS = [
    "Listá mis capas",
    "Shape de Uruguay",
    "Estilo por columna",
]

CHAT_QSS = """
    QTextEdit#chatView {
        border: 1px solid #d7dbe0;
        border-radius: 8px;
        padding: 6px;
        font-size: 13px;
        background: #f6f8fa;
    }
    QFrame#inputCard {
        border: 1px solid #c9ced6;
        border-radius: 10px;
        background: #ffffff;
    }
    QTextEdit#chatInput {
        border: none;
        background: transparent;
        font-size: 13px;
        padding: 2px;
    }
    QPushButton#sendBtn {
        background-color: #1a73e8;
        color: #ffffff;
        border: none;
        border-radius: 15px;
        min-width: 30px;
        max-width: 30px;
        min-height: 30px;
        max-height: 30px;
        font-size: 15px;
        font-weight: bold;
    }
    QPushButton#sendBtn:disabled {
        background-color: #c4c9cf;
    }
    QPushButton#stopBtn {
        color: #d93025;
        background: #ffffff;
        border: 1px solid #d93025;
        border-radius: 15px;
        min-width: 30px;
        max-width: 30px;
        min-height: 30px;
        max-height: 30px;
        font-size: 13px;
    }
    QPushButton#gearBtn {
        border: none;
        background: transparent;
        font-size: 15px;
        padding: 2px 6px;
    }
    QPushButton#gearBtn:checked {
        background: #e8eaed;
        border-radius: 6px;
    }
    QPushButton#gearBtn:hover {
        background: #f1f3f4;
    }
    QComboBox#pill {
        border: 1px solid #d0d5db;
        border-radius: 6px;
        padding: 2px 6px;
        background: #f6f8fa;
        font-size: 12px;
        min-height: 20px;
    }
    QComboBox#pill:hover {
        border-color: #1a73e8;
    }
    QToolButton#sugBtn {
        border: 1px solid #d0d5db;
        border-radius: 10px;
        padding: 3px 10px;
        background: #f6f8fa;
        font-size: 12px;
        color: #3c4043;
    }
    QToolButton#sugBtn:hover {
        border-color: #1a73e8;
        color: #1a73e8;
    }
    QPushButton#newBtn {
        border: 1px solid #d0d5db;
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 11px;
        background: #ffffff;
    }
    QPushButton#newBtn:hover {
        border-color: #1a73e8;
    }
"""

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
        self._bubble = None
        self._tool_results = []
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
        self.chat.notice.connect(self._on_chat_notice)

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
        tab.setStyleSheet(CHAT_QSS)

        header = QHBoxLayout()
        header.addStretch(1)
        self.chat_status = QLabel("Listo")
        self.chat_status.setStyleSheet("color:#5f6368; font-size:11px;")
        header.addWidget(self.chat_status)
        self.chat_new_btn = QPushButton("↻ Nueva")
        self.chat_new_btn.setObjectName("newBtn")
        self.chat_new_btn.setToolTip("Nueva conversación")
        self.chat_new_btn.clicked.connect(self._chat_new)
        header.addWidget(self.chat_new_btn)
        outer.addLayout(header)

        self.cfg_group = QGroupBox("Configuración del chat")
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
        self.cfg_url.setPlaceholderText("https://text.pollinations.ai/openai")
        cfg.addWidget(self.cfg_url, 2, 1, 1, 3)
        save_btn = QPushButton("Guardar")
        save_btn.clicked.connect(self._chat_save_config)
        cfg.addWidget(save_btn, 3, 3)
        self.cfg_hint = QLabel()
        self.cfg_hint.setWordWrap(True)
        self.cfg_hint.setStyleSheet("color:#5f6368; font-size:11px;")
        cfg.addWidget(self.cfg_hint, 3, 1, 1, 2)
        cfg.addWidget(QLabel("Prompt de sistema (agente Copla):"), 4, 0)
        self.cfg_prompt = QTextEdit()
        self.cfg_prompt.setPlainText(
            self.chat.config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        )
        self.cfg_prompt.setFixedHeight(70)
        cfg.addWidget(self.cfg_prompt, 4, 1, 1, 3)
        outer.addWidget(self.cfg_group)

        self.cfg_url.setText(self.chat.config.get("base_url", ""))
        self.cfg_model.setText(self.chat.config.get("model", ""))

        self.chat_view = _ChatView()
        self.chat_view.setObjectName("chatView")
        self.chat_view.setReadOnly(True)
        self.chat_view.setMinimumHeight(150)
        self.chat_view.anchor_clicked.connect(self._on_chat_anchor)
        outer.addWidget(self.chat_view, 1)

        self.suggestions = QWidget()
        sug_l = QHBoxLayout(self.suggestions)
        sug_l.setContentsMargins(0, 0, 0, 0)
        sug_l.setSpacing(6)
        self.suggestion_btns = []
        for sug in CHAT_SUGGESTIONS:
            sug_btn = QToolButton()
            sug_btn.setObjectName("sugBtn")
            sug_btn.setText(sug)
            sug_btn.clicked.connect(lambda _=False, t=sug: self._chat_suggestion(t))
            sug_l.addWidget(sug_btn)
            self.suggestion_btns.append(sug_btn)
        sug_l.addStretch(1)
        outer.addWidget(self.suggestions)

        card = QFrame()
        card.setObjectName("inputCard")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(10, 6, 10, 6)
        card_l.setSpacing(4)
        self.chat_input = _ChatInput()
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setPlaceholderText(
            "Preguntá lo que quieras… (Enter envía, Shift+Enter nueva línea)"
        )
        self.chat_input.setFixedHeight(56)
        self.chat_input.submitted.connect(self._chat_send)
        card_l.addWidget(self.chat_input)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.cfg_toggle = QPushButton("⚙")
        self.cfg_toggle.setObjectName("gearBtn")
        self.cfg_toggle.setCheckable(True)
        self.cfg_toggle.setToolTip("Configuración del chat (proveedor, modelo, prompt)")
        self.cfg_toggle.toggled.connect(self._chat_toggle_cfg)
        foot.addWidget(self.cfg_toggle)

        self.chat_agent = QComboBox()
        self.chat_agent.setObjectName("pill")
        for agent_name in AGENTS:
            self.chat_agent.addItem(agent_name)
        saved_agent = self.chat.config.get("agent") or "Copla"
        saved_idx = self.chat_agent.findText(saved_agent)
        self.chat_agent.setCurrentIndex(saved_idx if saved_idx >= 0 else 0)
        self.chat_agent.setToolTip(
            AGENTS[self.chat_agent.currentText()]["description"]
        )
        self.chat_agent.currentTextChanged.connect(self._chat_agent_changed)
        foot.addWidget(self.chat_agent)

        self.chat_model = QComboBox()
        self.chat_model.setObjectName("pill")
        for label in PRESETS:
            self.chat_model.addItem(label)
        self.chat_model.setToolTip("Proveedor y modelo (click para cambiar)")
        self.chat_model.currentTextChanged.connect(self._chat_model_changed)
        foot.addWidget(self.chat_model)

        foot.addStretch(1)
        self.chat_stop_btn = QPushButton("■")
        self.chat_stop_btn.setObjectName("stopBtn")
        self.chat_stop_btn.setToolTip("Detener respuesta")
        self.chat_stop_btn.clicked.connect(self._chat_stop)
        self.chat_stop_btn.setEnabled(False)
        self.chat_stop_btn.hide()
        foot.addWidget(self.chat_stop_btn)
        self.chat_send_btn = QPushButton("↑")
        self.chat_send_btn.setObjectName("sendBtn")
        self.chat_send_btn.setToolTip("Enviar (Enter)")
        self.chat_send_btn.clicked.connect(self._chat_send)
        foot.addWidget(self.chat_send_btn)
        card_l.addLayout(foot)
        outer.addWidget(card)

        self.agent_desc = QLabel(tab)
        self.agent_desc.setText(
            AGENTS[self.chat_agent.currentText()]["description"]
        )
        self.agent_desc.hide()

        self._chat_match_preset()
        configured = bool(self.chat.config.get("model"))
        self.cfg_group.setVisible(not configured)
        self.cfg_toggle.setChecked(not configured)

        self._stream_open = False
        self._bubble = None
        self._tool_results = []
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
            self.cfg_url.setFocus()
            self.chat_status.setText("Configurá proveedor y modelo")
            return
        if not self.chat.messages:
            self.chat_view.clear()
            self._bubble = None
            self._tool_results = []
            if hasattr(self, "suggestions"):
                self.suggestions.setVisible(False)
        self.chat_input.clear()
        self._chat_close_stream()
        self._chat_break(double=True)
        self._bubble_open("user")
        self._chat_append_plain(text)
        self._close_bubble()
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
        desc = AGENTS[name]["description"]
        self.agent_desc.setText(desc)
        self.chat_agent.setToolTip(desc)
        self.chat_status.setText("Agente: %s" % name)

    def _chat_suggestion(self, text):
        if self.chat is None or self.chat.busy:
            return
        self.chat_input.setPlainText(text)
        self._chat_send()

    def _chat_new(self):
        if self.chat is None or self.chat.busy:
            return
        self.chat.new_conversation()
        self._render_chat_history()
        self.chat_status.setText("Listo")

    def _chat_toggle_cfg(self, checked):
        self.cfg_group.setVisible(checked)

    def _apply_preset(self, label):
        preset = PRESETS.get(label)
        self.cfg_hint.setText(preset.get("hint", "") if preset else "")
        if preset and preset.get("base_url"):
            self.cfg_url.setText(preset["base_url"])
            if preset.get("model"):
                self.cfg_model.setText(preset["model"])

    def _commit_preset(self, label):
        self.chat.config["base_url"] = self.cfg_url.text().strip()
        self.chat.config["model"] = self.cfg_model.text().strip()
        save_config(self.chat.config)
        self.chat_status.setText("Proveedor: %s" % label)

    def _chat_preset_changed(self, label):
        if self._cfg_loading:
            return
        self._cfg_loading = True
        try:
            self._apply_preset(label)
            idx = self.chat_model.findText(label)
            if idx >= 0 and self.chat_model.currentIndex() != idx:
                self.chat_model.setCurrentIndex(idx)
        finally:
            self._cfg_loading = False
        self._commit_preset(label)

    def _chat_model_changed(self, label):
        if self._cfg_loading:
            return
        self._cfg_loading = True
        try:
            self._apply_preset(label)
            idx = self.cfg_preset.findText(label)
            if idx >= 0 and self.cfg_preset.currentIndex() != idx:
                self.cfg_preset.setCurrentIndex(idx)
        finally:
            self._cfg_loading = False
        self._commit_preset(label)

    def _chat_match_preset(self):
        self._cfg_loading = True
        try:
            matched = next(iter(PRESETS))
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
            for combo in (self.cfg_preset, self.chat_model):
                idx = combo.findText(matched)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
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
            self._bubble_open("ai")
            self._stream_open = True
        self._chat_append_plain(text)

    def _on_chat_tool_event(self, name, summary, status):
        self._chat_close_stream()
        self._close_bubble()
        result = ""
        if self.chat is not None and self.chat.messages:
            last = self.chat.messages[-1]
            if last.get("role") == "tool":
                result = str(last.get("content") or "")
        self._chat_break()
        self._tool_chip(summary, status, result)
        img = self._tool_image_path(name, result)
        if img:
            self._chat_break()
            self._bubble_open("ai")
            self._chat_append_html("<img src='%s' width='300'>" % img)
            self._close_bubble()
        self.chat_status.setText("Ejecutando %s…" % name)

    def _on_chat_finished(self, text):
        if self._stream_open and text and "*" in text and getattr(
            self, "_bubble_table", None
        ):
            if self._clear_bubble_content():
                self._chat_append_html(self._md_to_html(text))
        self._chat_close_stream()
        self.chat_status.setText("Listo")

    def _clear_bubble_content(self):
        table = getattr(self, "_bubble_table", None)
        if table is None:
            return False
        cell = table.cellAt(0, 0)
        cursor = QTextCursor(cell.firstCursorPosition())
        cursor.setPosition(
            cell.lastCursorPosition().position(), QTextCursor.KeepAnchor
        )
        cursor.removeSelectedText()
        self._bubble_cursor = QTextCursor(cursor)
        return True

    def _md_to_html(self, text):
        esc = html.escape(str(text))
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(
            r"`(.+?)`",
            r"<span style='font-family:Consolas,monospace'>\1</span>",
            esc,
        )
        esc = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", esc)
        esc = esc.replace("\n", "<br>")
        return esc

    def _on_chat_error(self, message):
        self._chat_close_stream()
        self._close_bubble()
        self._chat_break(double=True)
        self._bubble_open("err")
        self._chat_append_plain(str(message))
        self._close_bubble()
        self.chat_status.setText("Error")

    def _on_chat_notice(self, message):
        self._chat_close_stream()
        self._close_bubble()
        self.chat_status.setText(str(message))

    def _on_chat_busy(self, busy):
        self.chat_send_btn.setEnabled(not busy)
        self.chat_stop_btn.setEnabled(busy)
        self.chat_send_btn.setVisible(not busy)
        self.chat_stop_btn.setVisible(busy)
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
        if self._bubble:
            self._bubble_cursor.insertHtml(text)
        else:
            cursor = self.chat_view.textCursor()
            cursor.movePosition(cursor.End)
            self.chat_view.setTextCursor(cursor)
            self.chat_view.insertHtml(text)
        self._chat_scroll()

    def _chat_append_plain(self, text):
        self._chat_was_at_end = self._chat_at_end()
        if self._bubble:
            self._bubble_cursor.insertText(text, self._bubble_fmt)
        else:
            cursor = self.chat_view.textCursor()
            cursor.movePosition(cursor.End)
            self.chat_view.setTextCursor(cursor)
            self.chat_view.insertPlainText(text)
        self._chat_scroll()

    def _chat_close_stream(self):
        self._stream_open = False
        self._close_bubble()

    def _chat_break(self, double=False):
        if self.chat_view.document().characterCount() > 1:
            self._chat_append_html("<br>" + ("<br>" if double else ""))

    def _bubble_open(self, role):
        if self._bubble == role:
            return
        self._close_bubble()
        if role == "user":
            align, bg, border, color = Qt.AlignRight, "#e6f4ea", "#a8d5b5", "#137333"
        elif role == "err":
            align, bg, border, color = Qt.AlignLeft, "#fce8e6", "#f28b82", "#b3261e"
        else:
            align, bg, border, color = Qt.AlignLeft, "#ffffff", "#d7dbe0", "#202124"
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.chat_view.setTextCursor(cursor)
        outer_fmt = QTextTableFormat()
        outer_fmt.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        outer_fmt.setCellSpacing(0)
        outer_fmt.setBorderStyle(QTextTableFormat.BorderStyle_None)
        cursor.insertTable(1, 1, outer_fmt)
        inner_fmt = QTextTableFormat()
        inner_fmt.setBorder(1)
        inner_fmt.setBorderStyle(QTextTableFormat.BorderStyle_Solid)
        inner_fmt.setBorderBrush(QColor(border))
        inner_fmt.setPadding(6)
        inner_fmt.setCellSpacing(0)
        inner_fmt.setBackground(QColor(bg))
        inner_fmt.setAlignment(align)
        inner = cursor.insertTable(1, 1, inner_fmt)
        self._bubble_table = inner
        self._bubble_cursor = QTextCursor(cursor)
        char_fmt = QTextCharFormat()
        char_fmt.setForeground(QColor(color))
        char_fmt.setFontPointSize(13)
        self._bubble_fmt = char_fmt
        self.chat_view.setTextCursor(self._bubble_cursor)
        self._bubble = role

    def _close_bubble(self):
        if getattr(self, "_bubble", None):
            self._bubble = None
            self._bubble_cursor = None
            self._bubble_table = None
            cursor = self.chat_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.chat_view.setTextCursor(cursor)

    def _tool_chip(self, summary, status, result=""):
        idx = len(self._tool_results)
        self._tool_results.append(result)
        ok = status == "ok"
        color = "#3c4043" if ok else "#b42318"
        label = "ok" if ok else "error"
        self._chat_append_html(
            "<table width='100%%' cellspacing='0' cellpadding='0'><tr><td>"
            "<table cellspacing='0' cellpadding='3' border='1' bordercolor='#d0d5db' "
            "style='background:#eef1f4'><tr><td>"
            "<a href='copla-tool:%d' style='color:%s; text-decoration:none; "
            "font-family:Consolas,monospace; font-size:11px'>"
            "&#9656; %s — %s</a>"
            "</td></tr></table></td></tr></table>"
            % (idx, color, html.escape(str(summary)), label)
        )

    def _tool_image_path(self, name, result):
        if name not in ("render_map", "export_layout"):
            return None
        try:
            data = json.loads(result)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        path = str(data.get("saved") or data.get("file") or "")
        if not path.lower().endswith((".png", ".jpg", ".jpeg")):
            return None
        path = path.replace("\\", "/")
        return path if os.path.exists(path) else None

    def _on_chat_anchor(self, raw):
        raw = str(raw)
        if not raw.startswith("copla-tool:"):
            return
        try:
            idx = int(raw.split(":", 1)[1])
            result = self._tool_results[idx]
        except (ValueError, IndexError):
            return
        mb = QMessageBox(self.chat_view)
        mb.setWindowTitle("Resultado de la herramienta")
        mb.setTextFormat(Qt.PlainText)
        text = str(result or "(sin salida)")
        mb.setText(text if len(text) <= 900 else text[:900] + "…")
        mb.setDetailedText(text)
        mb.exec_()

    def _render_chat_history(self):
        self._stream_open = False
        self._bubble = None
        self._tool_results = []
        messages = self.chat.messages if self.chat is not None else []
        has_msgs = bool(messages)
        if hasattr(self, "suggestions"):
            self.suggestions.setVisible(not has_msgs)
        if not messages:
            self.chat_view.setHtml(
                "<div align='center'><br>"
                "<span style='color:#3c4043; font-size:13px'>"
                "<b>Empezá a pedirle a Copla</b></span><br><br>"
                "<span style='color:#666; font-size:12px'>"
                "Elegí el agente y el proveedor con los botones de abajo, "
                "y escribí tu pedido. La IA usa las herramientas de Copla "
                "para trabajar con capas, estilos y archivos de QGIS."
                "</span></div>"
            )
            return
        self.chat_view.clear()
        self._bubble = None
        self._tool_results = []
        pending = {}
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    pending[tc.get("id") or ""] = _safe_json(fn.get("arguments") or "{}")
                if not content:
                    continue
                self._close_bubble()
                self._chat_break(double=True)
                self._bubble_open("ai")
                if "*" in str(content):
                    self._chat_append_html(self._md_to_html(str(content)))
                else:
                    self._chat_append_plain(str(content))
                self._close_bubble()
            elif role == "user":
                if not content:
                    continue
                self._close_bubble()
                self._chat_break(double=True)
                self._bubble_open("user")
                self._chat_append_plain(str(content))
                self._close_bubble()
            elif role == "tool":
                args = pending.get(msg.get("tool_call_id") or "")
                summary = _args_summary(msg.get("name") or "", args)
                bad = str(content or "").startswith("ERROR[")
                result = str(content or "")
                self._close_bubble()
                self._chat_break()
                self._tool_chip(summary, "error" if bad else "ok", result)
                img = self._tool_image_path(msg.get("name") or "", result)
                if img:
                    self._chat_break()
                    self._bubble_open("ai")
                    self._chat_append_html("<img src='%s' width='300'>" % img)
                    self._close_bubble()
        self._close_bubble()
        self._chat_scroll(force=True)


class _ChatView(QTextEdit):
    anchor_clicked = pyqtSignal(str)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            cursor = self.cursorForPosition(event.pos())
            cursor.movePosition(cursor.StartOfChar, cursor.KeepAnchor)
            href = cursor.charFormat().anchorHref()
            if href:
                self.anchor_clicked.emit(href)
                return
        super().mouseReleaseEvent(event)


class _ChatInput(QTextEdit):
    submitted = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)
