"""Embedded chat engine for Copla.

Ships its own providers inside the plugin - no API key, no signup, no
registration anywhere: Pollinations in the cloud plus optional local
Ollama / LM Studio, with automatic failover between them. Talks to any
OpenAI-compatible chat completions endpoint with streaming and tool
calling against the local typed tool registry. There is no
arbitrary-code path: tools are resolved through tools.run_tool like the
HTTP bridge does.
"""

import json
import re
import os
import traceback

from qgis.core import QgsApplication, QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QTimer, QUrl, pyqtSignal, QObject
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from .tools import TOOLS, ToolError, run_tool

DEFAULT_BASE_URL = "https://text.pollinations.ai/openai"
DEFAULT_MODEL = "openai"

PRESETS = {
    "Pollinations (sin registro)": {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "hint": "Incluido en el plugin: funciona de entrada, sin registro, "
                "sin API key y sin tarjeta (nube comunitaria con límites de uso).",
    },
    "Ollama (local)": {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "hint": "Incluido: 100% local y sin registro. Requiere Ollama instalado "
                "y corriendo (`ollama pull qwen2.5:7b`).",
    },
    "LM Studio (local)": {
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "hint": "Incluido: 100% local y sin registro. Requiere LM Studio con el "
                "servidor local activado.",
    },
}

LEGACY_PROVIDER_URLS = {
    "https://api.groq.com/openai/v1",
    "https://openrouter.ai/api/v1",
    "https://generativelanguage.googleapis.com/v1beta/openai",
    "https://opencode.ai/zen/v1",
}


def _norm_base(url):
    return (url or "").rstrip("/")


def _bundled_after(current_base):
    """Labels of bundled providers after `current_base`, rotating. Empty
    for URLs outside the bundled set (no failover for custom endpoints)."""
    labels = list(PRESETS)
    index = {_norm_base(PRESETS[label]["base_url"]): i for i, label in enumerate(labels)}
    start = index.get(_norm_base(current_base))
    if start is None:
        return []
    return [labels[(start + i) % len(labels)] for i in range(1, len(labels))]


def _readonly_tool_names():
    return {name for name, meta in TOOLS.items() if not meta.get("mutates")}


AGENTS = {
    "Copla": {
        "description": "Asistente geoespacial completo: lee y modifica el proyecto (63 herramientas).",
        "prompt": "",
        "tools": "all",
    },
    "Explorador": {
        "description": "Solo lectura: capas, features, algoritmos y diagnóstico. No modifica nada.",
        "prompt": (
            "Sos el Explorador de Copla, un agente de SOLO LECTURA dentro de QGIS. "
            "Podés diagnosticar, listar e inspeccionar capas, consultar features, correr "
            "expresiones, buscar algoritmos y leer archivos, pero no modificás datos ni el "
            "proyecto. Si el usuario pide una acción que escribe o borra, explicale en una "
            "frase qué haría y sugerile cambiar de agente (Editor, Cartógrafo, Descargas o "
            "Copla). Respondé en el idioma del usuario, de forma concisa, con nombres de "
            "capas y conteos."
        ),
        "tools": "readonly",
    },
    "Cartógrafo": {
        "description": "Simbología, etiquetas, render de mapas y layouts.",
        "prompt": (
            "Sos el Cartógrafo de Copla: especialista en simbología, etiquetas y "
            "representación geográfica. Cuidá la legibilidad (contraste, jerarquía de "
            "clases, rotulación sin solapes), elegí renderers apropiados y renderizá o "
            "exportá mapas y layouts cuando se pida. Leés el proyecto pero no editás "
            "datos ni la estructura de capas: si hace falta editar, sugerile al usuario "
            "cambiar al agente Editor o Copla. Después de un cambio de estilo, explicá en "
            "una frase qué cambiaste y confirmá la acción. Respondé en el idioma del usuario."
        ),
        "tools": _readonly_tool_names() | {
            "set_renderer", "set_labels", "set_extent", "set_layer_visibility",
            "zoom_to_layer", "zoom_to_selection", "clear_selection", "add_basemap",
            "render_map", "list_layouts", "export_layout",
            "save_style", "load_style", "copy_style", "create_layout", "zoom_to_project",
        },
    },
    "Editor": {
        "description": "Alta, baja y edición de features y capas, y Processing.",
        "prompt": (
            "Sos el Editor de datos de Copla: manejás altas, bajas y modificaciones de "
            "features y capas, cambios de campos y la ejecución de algoritmos de "
            "Processing sobre datos. Antes de operaciones destructivas (borrar features "
            "o capas, sobrescribir archivos) confirmá con el usuario. Verificá el "
            "resultado de cada edición con un conteo o consulta. No cambies estilos ni "
            "descargues datos: para eso están los agentes Cartógrafo y Descargas. "
            "Respondé en el idioma del usuario, de forma concisa."
        ),
        "tools": _readonly_tool_names() | {
            "add_layer", "create_layer", "remove_layer", "remove_group", "rename_layer",
            "create_group", "rename_group", "move_layer",
            "add_features", "delete_features", "update_attributes",
            "add_field", "remove_field", "rename_field", "calculate_field",
            "run_algorithm", "save_layer_as", "save_project", "load_project",
            "set_project_crs",
            "buffer", "reproject_layer", "clip", "intersection", "dissolve", "fix_geometries",
        },
    },
    "Descargas y archivos": {
        "description": "Descarga de datos de internet y gestión de archivos locales.",
        "prompt": (
            "Sos el agente de Descargas y archivos de Copla: bajás capas y datos de "
            "internet, listás y movés archivos y creás archivos geográficos nuevos "
            "(gpkg/shp). Siempre informá la ruta de destino y verificá que la descarga "
            "o copia haya quedado bien (tamaño, apertura o conteo). No edites features "
            "ni estilos: para eso están los agentes Editor y Cartógrafo. Respondé en el "
            "idioma del usuario, de forma concisa."
        ),
        "tools": _readonly_tool_names() | {
            "download_layer", "move_file", "create_layer", "save_layer_as", "add_layer",
            "copy_file", "download_file", "delete_file",
        },
    },
    "General": {
        "description": "Conversación general sin herramientas de QGIS.",
        "prompt": (
            "Sos General, un asistente conversacional dentro de QGIS sin herramientas: "
            "respondés preguntas generales, explicás conceptos y ayudás a pensar, sin "
            "ejecutar acciones sobre el proyecto. Si el usuario necesita operar sobre el "
            "proyecto, indicale que elija el agente Copla (o Explorador, Cartógrafo, "
            "Editor o Descargas) desde el selector de agentes. Respondé en el idioma del "
            "usuario, de forma concisa."
        ),
        "tools": [],
    },
}

DEFAULT_SYSTEM_PROMPT = (
    "Sos Copla, un asistente geoespacial embebido dentro de QGIS Desktop. "
    "Tenés herramientas tipadas para controlar el proyecto del usuario: capas, "
    "edición de features, simbología y etiquetas, Processing, descarga de datos, "
    "archivos y exportación. Usá las herramientas siempre que ayuden en lugar de "
    "adivinar; no podés ejecutar código arbitrario ni Python. "
    "Respondé en el idioma del usuario, de forma concisa y concreta "
    "(nombres de capas, conteos, rutas). Confirmá las acciones que modifican datos."
)

MAX_TOOL_ITERATIONS = 8
MAX_TOOL_RESULT = 6000
HISTORY_LIMIT = 40
REQUEST_TIMEOUT_MS = 180000


def _profile_dir():
    return QgsApplication.qgisSettingsDirPath()


def _config_path():
    return os.path.join(_profile_dir(), "copla_chat.json")


def _history_path():
    return os.path.join(_profile_dir(), "copla_chat_history.json")


def load_config():
    config = {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "agent": "Copla",
    }
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            config.update({k: v for k, v in saved.items() if k in config})
    except (OSError, ValueError):
        pass
    if _norm_base(config["base_url"]) in LEGACY_PROVIDER_URLS:
        config["base_url"] = DEFAULT_BASE_URL
        config["model"] = DEFAULT_MODEL
    if not config["system_prompt"]:
        config["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    if config.get("agent") not in AGENTS:
        config["agent"] = "Copla"
    return config


def save_config(config):
    with open(_config_path(), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)


def _load_history():
    try:
        with open(_history_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")]
    except (OSError, ValueError):
        pass
    return []


def _save_history(messages):
    try:
        with open(_history_path(), "w", encoding="utf-8") as fh:
            json.dump(messages, fh, ensure_ascii=False)
    except OSError:
        pass


def tool_schemas():
    schemas = []
    for name, meta in TOOLS.items():
        props = {}
        required = []
        for pname, p in meta.get("params", {}).items():
            prop = {"type": p.get("type", "string"), "description": p.get("description", "")}
            if p.get("enum"):
                prop["enum"] = list(p["enum"])
            if p.get("type") == "array":
                prop["items"] = {}
            props[pname] = prop
            if p.get("required"):
                required.append(pname)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": meta.get("description", ""),
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        })
    return schemas


def agent_schemas(agent_name):
    agent = AGENTS.get(agent_name or "Copla") or AGENTS["Copla"]
    spec = agent.get("tools", "all")
    schemas = tool_schemas()
    if spec == "all":
        return schemas
    if spec == "readonly":
        allowed = _readonly_tool_names()
    elif isinstance(spec, str):
        allowed = _readonly_tool_names()
    else:
        allowed = set(spec)
    return [s for s in schemas if s["function"]["name"] in allowed]


def _truncate(value, limit=MAX_TOOL_RESULT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated %d chars]" % (len(text) - limit)


def _args_summary(name, args):
    parts = []
    if isinstance(args, dict):
        for key, value in list(args.items())[:4]:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            if len(text) > 60:
                text = text[:57] + "..."
            parts.append("%s=%s" % (key, text))
    return "%s(%s)" % (name, ", ".join(parts))


class ChatEngine(QObject):
    token_received = pyqtSignal(str)
    assistant_finished = pyqtSignal(str)
    tool_event = pyqtSignal(str, str, str)
    error_raised = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    notice = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.messages = _load_history()
        self._nam = QgsNetworkAccessManager.instance()
        self._reply = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._busy = False
        self._stopped = False
        self._use_stream = True
        self._round_start = 0
        self._iteration = 0
        self._retries429 = 0
        self._buf = ""
        self._content_parts = []
        self._tool_acc = {}
        self._finish_reason = None
        self._streaming = True
        self._tried_bases = set()
        self._active_base = None
        self._active_model = None

    # ------------------------------------------------------------- public

    @property
    def busy(self):
        return self._busy

    def send(self, text):
        if self._busy:
            self.error_raised.emit("Ya hay una respuesta en curso; usá Detener o esperá.")
            return
        if not self.config.get("model") or not self.config.get("base_url"):
            self.error_raised.emit("Configurá proveedor y modelo en la pestaña Configuración del chat.")
            return
        text = (text or "").strip()
        if not text:
            return
        self._round_start = len(self.messages)
        self._retries429 = 0
        self._active_base = None
        self._active_model = None
        self._tried_bases = {_norm_base(self.config.get("base_url"))}
        self.messages.append({"role": "user", "content": text})
        _save_history(self.messages)
        self._start_round(0)

    def stop(self):
        if self._reply is not None:
            self._stopped = True
            self._reply.abort()

    def new_conversation(self):
        self.messages = []
        _save_history(self.messages)

    # ------------------------------------------------------------- internals

    def _set_busy(self, value):
        if self._busy != value:
            self._busy = value
            self.busy_changed.emit(value)

    def _start_round(self, iteration):
        self._iteration = iteration
        self._stopped = False
        self._content_parts = []
        self._tool_acc = {}
        self._finish_reason = None
        self._buf = ""
        self._streaming = self._use_stream
        self._set_busy(True)
        self._post_chat(stream=self._use_stream)

    def _messages_payload(self):
        payload = []
        agent = AGENTS.get(self.config.get("agent") or "Copla") or AGENTS["Copla"]
        prompt = (
            agent.get("prompt")
            or self.config.get("system_prompt")
            or DEFAULT_SYSTEM_PROMPT
        )
        payload.append({"role": "system", "content": prompt})
        trimmed = list(self.messages)
        while len(trimmed) > HISTORY_LIMIT:
            trimmed.pop(0)
            while trimmed and trimmed[0].get("role") != "user":
                trimmed.pop(0)
        payload.extend(trimmed)
        return payload

    def _post_chat(self, stream):
        base = _norm_base(self._active_base or self.config.get("base_url"))
        url = base + "/chat/completions"
        body = {
            "model": self._active_model or self.config.get("model"),
            "messages": self._messages_payload(),
            "stream": stream,
            "temperature": 0.3,
        }
        tools = agent_schemas(self.config.get("agent"))
        if tools:
            body["tools"] = tools
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        data = QByteArray(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        self._streaming = stream
        self._reply = self._nam.post(request, data)
        self._reply.finished.connect(self._on_finished)
        if stream:
            self._reply.readyRead.connect(self._on_ready_read)
        self._timer.start(REQUEST_TIMEOUT_MS)

    def _on_timeout(self):
        if self._reply is not None:
            self._stopped = True
            self._reply.abort()

    def _on_ready_read(self):
        if self._reply is None or not self._streaming:
            return
        self._buf += bytes(self._reply.readAll()).decode("utf-8", "replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            self._handle_sse(data)

    def _handle_sse(self, data):
        try:
            chunk = json.loads(data)
        except ValueError:
            return
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                self._content_parts.append(content)
                self.token_received.emit(content)
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                slot = self._tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn.get("name")
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]

    def _on_finished(self):
        reply = self._reply
        if reply is None:
            return
        try:
            self._timer.stop()
            if self._streaming:
                leftover = bytes(reply.readAll()).decode("utf-8", "replace")
                if leftover:
                    self._buf += leftover
                    for line in self._buf.split("\n"):
                        line = line.strip()
                        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                            self._handle_sse(line[5:].strip())
                self._buf = ""
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            http_error = reply.error() != QNetworkReply.NoError
            err_str = reply.errorString()
            body = bytes(reply.readAll()).decode("utf-8", "replace") if not self._streaming else ""
            stopped = self._stopped
        finally:
            reply.deleteLater()
            self._reply = None
            try:
                reply.readyRead.disconnect(self._on_ready_read)
            except (TypeError, RuntimeError):
                pass
            try:
                reply.finished.disconnect(self._on_finished)
            except (TypeError, RuntimeError):
                pass

        if http_error and not stopped:
            if status:
                detail = self._extract_error(body)
                message = (
                    ("HTTP %s: %s" % (status, detail)) if detail else ("HTTP %s" % status)
                )
            else:
                message = err_str or "sin conexión con el proveedor"
            if status == 429 and not stopped and self._retries429 < 3:
                self._retries429 += 1
                QTimer.singleShot(self._retry_delay_ms(message), self._retry_429)
                return
            transient = status is None or (
                isinstance(status, int)
                and (status == 400 or status >= 500)
            )
            if (
                self._streaming
                and self._use_stream
                and not self._stopped
                and transient
            ):
                self._use_stream = False
                self._resume_without_stream()
                return
            self._fail_or_failover(message)
            return
        if stopped:
            partial = "".join(self._content_parts)
            if partial:
                self.messages.append({"role": "assistant", "content": partial})
                _save_history(self.messages)
                self.assistant_finished.emit(partial)
            self._set_busy(False)
            return

        if not self._streaming:
            if not self._ingest_non_stream(body):
                return

        tool_calls = [self._tool_acc[i] for i in sorted(self._tool_acc)]
        content = "".join(self._content_parts)
        if tool_calls and self._iteration < MAX_TOOL_ITERATIONS:
            self._run_tool_calls(tool_calls, content)
            return
        if tool_calls and self._iteration >= MAX_TOOL_ITERATIONS:
            note = "\n\n[Alcanzé el límite de %d pasos de herramientas en esta ronda]" % MAX_TOOL_ITERATIONS
            content += note
            self.messages.append({"role": "assistant", "content": content})
            _save_history(self.messages)
            self.assistant_finished.emit(content)
            self._set_busy(False)
            return
        self.messages.append({"role": "assistant", "content": content})
        _save_history(self.messages)
        self.assistant_finished.emit(content)
        self._set_busy(False)

    def _resume_without_stream(self):
        self._content_parts = []
        self._tool_acc = {}
        self._buf = ""
        self._streaming = False
        self._post_chat(stream=False)

    def _retry_delay_ms(self, message):
        match = re.search(r"in (\d+(?:\.\d+)?)s", message or "")
        if match:
            seconds = float(match.group(1)) + 2.0
        else:
            seconds = 15.0
        return int(max(5.0, min(60.0, seconds)) * 1000)

    def _retry_429(self):
        if self._stopped or not self._busy:
            if self._stopped:
                self._set_busy(False)
            return
        self._start_round(self._iteration)

    def _ingest_non_stream(self, body):
        try:
            data = json.loads(body)
        except ValueError:
            self._fail_or_failover("Respuesta no es JSON: %s" % body[:200])
            return False
        for choice in data.get("choices") or []:
            message = choice.get("message") or {}
            content = message.get("content")
            if content:
                self._content_parts.append(content)
                self.token_received.emit(content)
            for i, tc in enumerate(message.get("tool_calls") or []):
                fn = tc.get("function") or {}
                self._tool_acc[i] = {
                    "id": tc.get("id") or "call_%d" % i,
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                }
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return True

    def _extract_error(self, body):
        if not body:
            return ""
        try:
            data = json.loads(body)
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or "")[:300]
            if isinstance(err, str):
                return err[:300]
        except ValueError:
            pass
        return body[:300]

    def _run_tool_calls(self, tool_calls, content):
        if content:
            self.messages.append({"role": "assistant", "content": content})
        else:
            self.messages.append({"role": "assistant", "content": None})
        assistant_msg = self.messages[-1]
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id") or "call_%d" % i,
                "type": "function",
                "function": {"name": tc.get("name") or "", "arguments": tc.get("arguments") or "{}"},
            }
            for i, tc in enumerate(tool_calls)
        ]
        for i, tc in enumerate(tool_calls):
            name = tc.get("name") or ""
            raw_args = tc.get("arguments") or "{}"
            summary = _args_summary(name, _safe_json(raw_args))
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                result = run_tool(name, args)
                text = _truncate(json.dumps(result, ensure_ascii=False, default=str))
                status = "ok"
            except ToolError as exc:
                text = "ERROR[%s]: %s" % (exc.code, exc)
                status = "error"
            except (ValueError, TypeError) as exc:
                text = "ERROR[bad_args]: invalid JSON arguments: %s" % exc
                status = "error"
            except Exception as exc:
                text = "ERROR[tool_error]: %s" % exc
                status = "error"
                traceback.print_exc()
            self.messages.append({
                "role": "tool",
                "tool_call_id": assistant_msg["tool_calls"][i]["id"],
                "name": name,
                "content": text,
            })
            self.tool_event.emit(name, summary, status)
        self._start_round(self._iteration + 1)

    def _fail_or_failover(self, message):
        chain = [
            label
            for label in _bundled_after(self._active_base or self.config.get("base_url"))
            if _norm_base(PRESETS[label]["base_url"]) not in self._tried_bases
        ]
        if not chain:
            self._fail("El proveedor respondió error: %s" % message)
            return
        current = self._active_base or self.config.get("base_url") or ""
        current_label = next(
            (
                label
                for label, preset in PRESETS.items()
                if _norm_base(preset["base_url"]) == _norm_base(current)
            ),
            current,
        )
        label = chain[0]
        self._tried_bases.add(_norm_base(PRESETS[label]["base_url"]))
        self._active_base = PRESETS[label]["base_url"]
        self._active_model = PRESETS[label]["model"]
        self._retries429 = 0
        self.notice.emit(
            "%s no responde (%s) → probando %s…" % (current_label, message, label)
        )
        self._start_round(self._iteration)

    def _fail(self, message):
        self.messages = self.messages[: self._round_start]
        _save_history(self.messages)
        self.error_raised.emit(message)
        self._set_busy(False)


def _safe_json(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}
