"""
ccbb.web.handler — HTTP 请求处理 + SSE 推送

内嵌于 daemon 进程，通过首行检测区分 HTTP 和 TCP 连接。
提供配对页面、API 端点和 SSE 实时事件流。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("ccbb.web")


class HttpRequest:
    __slots__ = ("method", "path", "query", "headers", "body")

    def __init__(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body


async def read_http_request(reader: asyncio.StreamReader, first_line: str) -> HttpRequest:
    """解析 HTTP 请求（首行已读取）。"""
    parts = first_line.split(" ", 2)
    method = parts[0]
    raw_path = parts[1] if len(parts) > 1 else "/"

    parsed = urlparse(raw_path)
    path = parsed.path
    query = parse_qs(parsed.query)

    # 读取 headers
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        line_str = line.decode("utf-8", errors="replace").strip()
        if ":" in line_str:
            key, val = line_str.split(":", 1)
            headers[key.strip().lower()] = val.strip()

    # 读取 body
    body = b""
    content_length = int(headers.get("content-length", "0"))
    if content_length > 0:
        body = await reader.readexactly(content_length)

    return HttpRequest(method, path, query, headers, body)


class WebHandler:
    def __init__(self, bridge: object) -> None:
        self._bridge = bridge

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     first_line: str) -> None:
        try:
            req = await read_http_request(reader, first_line)

            if req.method == "GET" and req.path == "/pair":
                await self._serve_static(writer, "pair.html", "text/html; charset=utf-8")
            elif req.method == "GET" and req.path.startswith("/static/"):
                await self._serve_static_path(writer, req.path)
            elif req.method == "POST" and req.path == "/api/pair":
                await self._api_pair(writer, req)
            elif req.method == "GET" and req.path == "/api/stream":
                await self._api_stream(writer, req)
            elif req.method == "POST" and req.path.startswith("/api/decide"):
                await self._api_decide(writer, req)
            else:
                self._send_response(writer, 404, "Not Found")
        except Exception as e:
            try:
                self._send_response(writer, 500, f"Error: {e}")
            except Exception:
                pass
        finally:
            try:
                if not writer.is_closing():
                    writer.close()
            except Exception:
                pass

    # ── 静态文件 ────────────────────────────────────────────────────────────

    async def _serve_static(self, writer: asyncio.StreamWriter, filename: str,
                            content_type: str) -> None:
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        filepath = os.path.join(static_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            self._send_response(writer, 200, content, content_type)
        except FileNotFoundError:
            self._send_response(writer, 404, "Not Found")

    _MIME_TYPES = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml; charset=utf-8",
    }

    async def _serve_static_path(self, writer: asyncio.StreamWriter,
                                 url_path: str) -> None:
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        # URL: /static/buddies/cat.js → rel: buddies/cat.js
        rel = os.path.normpath(url_path[len("/static/"):])
        if rel.startswith("..") or os.path.isabs(rel):
            self._send_response(writer, 403, "Forbidden")
            return
        filepath = os.path.join(static_dir, rel)
        _, ext = os.path.splitext(filepath)
        content_type = self._MIME_TYPES.get(ext, "application/octet-stream")
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            self._send_response(writer, 200, content, content_type)
        except FileNotFoundError:
            self._send_response(writer, 404, "Not Found")

    # ── API: 配对 ───────────────────────────────────────────────────────────

    async def _api_pair(self, writer: asyncio.StreamWriter, req: HttpRequest) -> None:
        try:
            body = json.loads(req.body.decode("utf-8"))
        except Exception:
            self._send_json(writer, 400, {"error": "Invalid JSON"})
            return

        payload = body.get("data", body)
        code = payload.get("pairing_code") or payload.get("code") or ""
        session_id = self._bridge._pairing_index.get(code)
        if not session_id:
            self._send_json(writer, 404, {"error": "Invalid pairing code"})
            return

        session = self._bridge._sessions.get(session_id)
        if not session:
            self._send_json(writer, 404, {"error": "Session not found"})
            return

        self._send_json(writer, 200, {"session_id": session_id, "pairing_code": code})

    # ── API: SSE 事件流 ─────────────────────────────────────────────────────

    async def _api_stream(self, writer: asyncio.StreamWriter, req: HttpRequest) -> None:
        session_id = req.query.get("session_id", [""])[0]
        if not session_id:
            self._send_response(writer, 400, "Missing session_id")
            return

        session = self._bridge._sessions.get(session_id)
        if not session:
            self._send_response(writer, 404, "Session not found")
            return

        queue: asyncio.Queue = asyncio.Queue()
        session.web_queues.append(queue)

        try:
            # SSE 头
            writer.write(
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "\r\n"
                .encode("utf-8")
            )
            await writer.drain()

            # 如果有挂起的请求，推送队首
            if session.pending_requests:
                first_req = next(iter(session.pending_requests.values()))
                await self._sse_push(writer, "request", first_req.raw)

            # 等待事件
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    await self._sse_push(writer, event["type"], event["data"])
                    if event["type"] == "session_end":
                        break
                except asyncio.TimeoutError:
                    await self._sse_push(writer, "ping", {})
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass
        finally:
            if queue in session.web_queues:
                session.web_queues.remove(queue)

    # ── API: 审批决策 ───────────────────────────────────────────────────────

    async def _api_decide(self, writer: asyncio.StreamWriter, req: HttpRequest) -> None:
        session_id = req.query.get("session_id", [""])[0]
        if not session_id:
            self._send_json(writer, 400, {"error": "Missing session_id"})
            return

        try:
            body = json.loads(req.body.decode("utf-8"))
        except Exception:
            self._send_json(writer, 400, {"error": "Invalid JSON"})
            return

        decision = body.get("data", body)
        if "behavior" not in decision:
            self._send_json(writer, 400, {"error": "Missing behavior"})
            return

        logger.info(f"Web 审批决策: session={session_id[:8]}... decision={decision}")

        rid = self._bridge._resolve_decision(session_id, decision)
        if rid:
            self._send_json(writer, 200, {"ok": True})
        else:
            self._send_json(writer, 404, {"error": "No pending request"})

    # ── 响应工具 ────────────────────────────────────────────────────────────

    async def _sse_push(self, writer: asyncio.StreamWriter, event_type: str,
                        data: object) -> None:
        msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        writer.write(msg.encode("utf-8"))
        await writer.drain()

    def _send_response(self, writer: asyncio.StreamWriter, status: int,
                       body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        reasons = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
        reason = reasons.get(status, "OK")
        body_bytes = body.encode("utf-8")
        resp = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        ).encode("utf-8") + body_bytes
        writer.write(resp)

    def _send_json(self, writer: asyncio.StreamWriter, status: int,
                   data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False)
        self._send_response(writer, status, body, "application/json; charset=utf-8")
