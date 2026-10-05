"""HTTP client for the Copla plugin."""

import json
import os
import urllib.error
import urllib.request

from .discover import find_config

DEFAULT_TIMEOUT = 30
ALGORITHM_TIMEOUT = 900


class CoplaError(Exception):
    pass


class CoplaClient:
    def __init__(self, port=None, token=None, base_url=None):
        self._explicit = base_url is not None
        self.base_url = base_url
        self.port = port
        self.token = token

    def _ensure_config(self):
        if self.base_url:
            return
        env_port = os.environ.get("COPLA_PORT")
        env_token = os.environ.get("COPLA_TOKEN")
        if env_port and env_token:
            self.port = int(env_port)
            self.token = env_token
            self.base_url = "http://127.0.0.1:%s" % env_port
            return
        config = find_config()
        if config is None:
            raise CoplaError(
                "QGIS no detectado. Abrí QGIS con el plugin 'Copla' activo "
                "(Complementos > Administrar e instalar complementos > Instalados) "
                "y verificá que el servidor diga 'Activo'. "
                "Si usás varias cosas raras de rutas: exportá COPLA_CONFIG "
                "apuntando al copla.json de tu perfil. "
                "(English: QGIS not detected. Open QGIS with the Copla plugin enabled.)"
            )
        self.port = int(config["port"])
        self.token = config["token"]
        self.base_url = "http://127.0.0.1:%d" % self.port

    def ping(self, timeout=5):
        self._ensure_config()
        try:
            with urllib.request.urlopen(self.base_url + "/v1/ping", timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise CoplaError(
                "El servidor de QGIS no responde en %s (%s). "
                "¿QGIS sigue abierto? ¿El servidor del dock dice 'Activo'?"
                % (self.base_url, exc)
            ) from exc

    def list_tools(self, timeout=10):
        self._ensure_config()
        request = urllib.request.Request(
            self.base_url + "/v1/tools",
            headers={"X-Copla-Token": self.token or ""},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise CoplaError("No se pudo contactar a QGIS en %s (%s)" % (self.base_url, exc)) from exc
        if not data.get("ok"):
            error = data.get("error") or {}
            raise CoplaError(error.get("message") or "error desconocido en QGIS")
        return data.get("tools", [])

    def call(self, tool, args=None, timeout=DEFAULT_TIMEOUT):
        self._ensure_config()
        payload = json.dumps({"tool": tool, "args": args or {}}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1/rpc",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Copla-Token": self.token or "",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(body)
            except ValueError:
                raise CoplaError("HTTP %d: %s" % (exc.code, body)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CoplaError(
                "No se pudo contactar a QGIS en %s (%s). "
                "¿Está QGIS abierto y el plugin Copla activo?" % (self.base_url, exc)
            ) from exc
        if not data.get("ok"):
            error = data.get("error") or {}
            raise CoplaError(error.get("message") or "error desconocido en QGIS")
        return data.get("result")
