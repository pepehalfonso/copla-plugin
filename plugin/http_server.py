"""Minimal HTTP/1.1 server for Copla.

Runs on the Qt main thread via QTcpServer (all QGIS objects must be
accessed from the main thread). One request per connection: the server
replies with ``Connection: close`` and closes, which keeps parsing
trivial and curl/debugging easy.

Security model:
- binds to 127.0.0.1 only
- random token required (``X-Copla-Token``) on every RPC call
- requests carrying a browser ``Origin`` header are rejected
"""

import json
import traceback as tb_module

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer

MAX_HEADER = 16 * 1024
MAX_BODY = 8 * 1024 * 1024
IDLE_TIMEOUT_MS = 30000
ALLOWED_ORIGINS = {"", "null", "http://localhost", "http://127.0.0.1"}


class HttpError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class CoplaHttpServer:
    def __init__(self, port, token, dispatch, parent=None, on_log=None):
        self.port = port
        self.token = token
        self.dispatch = dispatch
        self.on_log = on_log or (lambda msg: None)
        self._server = QTcpServer(parent)
        self._server.newConnection.connect(self._on_new_connection)
        self._states = {}

    def start(self):
        if self._server.isListening():
            return True
        ok = self._server.listen(QHostAddress("127.0.0.1"), self.port)
        if ok:
            self.on_log("listening on 127.0.0.1:%d" % self.port)
        else:
            self.on_log("listen failed: %s" % self._server.errorString())
        return ok

    def is_listening(self):
        return self._server.isListening()

    def stop(self):
        for state in list(self._states.values()):
            try:
                state["sock"].abort()
            except Exception:
                pass
        self._states.clear()
        self._server.close()

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._states[sock] = {"buf": bytearray()}
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
            QTimer.singleShot(IDLE_TIMEOUT_MS, lambda s=sock: self._expire(s))

    def _expire(self, sock):
        state = self._states.pop(sock, None)
        if state is not None:
            try:
                sock.abort()
            except Exception:
                pass

    def _on_disconnected(self, sock):
        self._states.pop(sock, None)
        try:
            sock.deleteLater()
        except Exception:
            pass

    def _on_ready(self, sock):
        state = self._states.get(sock)
        if state is None:
            return
        state["buf"].extend(bytes(sock.readAll()))
        if len(state["buf"]) > MAX_HEADER + MAX_BODY:
            self._respond(sock, 413, {"ok": False, "error": {"code": "too_large", "message": "request too large"}})
            return
        try:
            request = self._parse(state["buf"])
        except HttpError as exc:
            self._respond(sock, exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
            return
        if request is None:
            return
        self._handle(sock, request)

    def _parse(self, buf):
        marker = buf.find(b"\r\n\r\n")
        if marker < 0:
            if len(buf) > MAX_HEADER:
                raise HttpError(431, "headers_too_large", "headers too large")
            return None
        head = bytes(buf[:marker]).decode("iso-8859-1", "replace")
        lines = head.split("\r\n")
        try:
            method, path, _version = lines[0].split(" ")
        except ValueError:
            raise HttpError(400, "bad_request", "malformed request line")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = 0
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError:
                raise HttpError(400, "bad_request", "invalid content-length")
        if length > MAX_BODY:
            raise HttpError(413, "too_large", "body too large")
        start = marker + 4
        if len(buf) < start + length:
            return None
        body = bytes(buf[start:start + length])
        return {"method": method, "path": path, "headers": headers, "body": body}

    def _handle(self, sock, request):
        headers = request["headers"]
        origin = headers.get("origin", "")
        if origin and origin not in ALLOWED_ORIGINS and not origin.startswith("http://localhost:") and not origin.startswith("http://127.0.0.1:"):
            self._respond(sock, 403, {"ok": False, "error": {"code": "forbidden_origin", "message": "browser origins are not allowed"}})
            return
        path = request["path"].split("?")[0]

        if request["method"] == "GET" and path == "/v1/ping":
            self._respond(sock, 200, {"ok": True, "service": "copla"})
            return

        if headers.get("x-copla-token", "") != self.token:
            self._respond(sock, 401, {"ok": False, "error": {"code": "unauthorized", "message": "missing or invalid X-Copla-Token"}})
            return

        if request["method"] == "GET" and path == "/v1/tools":
            self._respond(sock, 200, {"ok": True, "tools": self.dispatch("list_tools", {})})
            return

        if request["method"] != "POST" or path != "/v1/rpc":
            self._respond(sock, 404, {"ok": False, "error": {"code": "not_found", "message": "use POST /v1/rpc"}})
            return

        try:
            payload = json.loads(request["body"].decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._respond(sock, 400, {"ok": False, "error": {"code": "bad_json", "message": "body is not valid JSON"}})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("tool"), str):
            self._respond(sock, 400, {"ok": False, "error": {"code": "bad_request", "message": 'expected {"tool": "...", "args": {...}}'}})
            return
        args = payload.get("args", {})
        if not isinstance(args, dict):
            self._respond(sock, 400, {"ok": False, "error": {"code": "bad_request", "message": "'args' must be an object"}})
            return
        try:
            result = self.dispatch(payload["tool"], args)
        except Exception as exc:
            code = getattr(exc, "code", "internal_error")
            message = str(exc) or exc.__class__.__name__
            error = {"code": code, "message": message}
            if code == "internal_error":
                error["traceback"] = tb_module.format_exc()
            status = 400 if code in ("bad_args", "unknown_tool", "not_found", "ambiguous_layer") else 500
            if code == "run_error":
                status = 500
            self._respond(sock, status, {"ok": False, "error": error})
            return
        self._respond(sock, 200, {"ok": True, "result": result})

    def _respond(self, sock, status, payload):
        reason = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 413: "Payload Too Large", 431: "Request Header Fields Too Large",
            500: "Internal Server Error",
        }.get(status, "Error")
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        head = (
            "HTTP/1.1 %d %s\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n" % (status, reason, len(body))
        ).encode("iso-8859-1")
        try:
            self._states.pop(sock, None)
            sock.write(head + body)
            sock.disconnectFromHost()
        except Exception:
            pass
