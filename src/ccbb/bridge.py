"""
ccbb.bridge — 守护进程核心

架构说明
--------
支持多个 Claude Code 会话与多个审批设备配对：

  Claude Code 会话 A ──pairing_code_A── 设备 A
  Claude Code 会话 B ──pairing_code_B── 设备 B

配对机制：
1. SessionStart hook 触发时，Bridge 注册会话并生成 6 位配对码
2. Hook 将配对码显示在 Claude Code 终端
3. 用户在设备上输入配对码完成配对
4. 审批请求只发送给配对的设备，决策只返回给配对的 Hook
5. SessionEnd hook 触发时清理配对并通知设备

关键设计
--------
1. 超时由 Claude Code 管理，hook 断开时 bridge 自动感知
2. Fail-open：bridge 未运行时，CC 走自己的权限对话框
3. SessionEnd 火速清理（不 await 通知）
4. 跨平台：Windows、macOS、Linux 统一使用 TCP
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import socket
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Optional, Set, Dict

# ── 常量 ───────────────────────────────────────────────────────────────────
TCP_HOST_DEFAULT = "0.0.0.0"
TCP_PORT_DEFAULT = 9876
ENTRIES_MAX = 5
HINT_MAX = 200
_HINT_KEYS = ("command", "file_path", "url", "path", "pattern", "query", "prompt", "input")

logger = logging.getLogger("ccbb.bridge")


def _make_hint(tool_input: object) -> str:
    """从 tool_input 中提取操作摘要"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:HINT_MAX]
    for key in _HINT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val[:HINT_MAX]
    try:
        return json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False)[:HINT_MAX]
    except Exception:
        return str(tool_input)[:HINT_MAX]


# ── 工具函数 ────────────────────────────────────────────────────────────────


def generate_pairing_code() -> str:
    """生成随机6位配对码"""
    return str(random.randint(100000, 999999))


def truncate(text: str, max_len: int = 60) -> str:
    """截断文本，保护设备显示。"""
    return text[:max_len]


# ── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class PendingRequest:
    id: str
    decision_future: asyncio.Future
    raw: dict  # hook 发来的完整请求，透传给设备/web


@dataclass
class Session:
    """一个 Claude Code 会话"""
    session_id: str
    pairing_code: str
    paired_devices: Set["DeviceConnection"] = field(default_factory=set)
    pending_requests: Dict[str, PendingRequest] = field(default_factory=dict)
    entries: list[str] = field(default_factory=list)
    web_queues: list = field(default_factory=list)  # SSE 订阅者


@dataclass
class DeviceConnection:
    """表示一个连接的审批设备"""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr: tuple
    uid: str
    session_id: Optional[str] = None

    def __hash__(self) -> int:
        return hash(self.uid)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DeviceConnection):
            return self.uid == other.uid
        return False


# ── Bridge 主类 ─────────────────────────────────────────────────────────────
class Bridge:
    def __init__(self, host: str = "0.0.0.0", port: int = TCP_PORT_DEFAULT) -> None:
        self._sessions: Dict[str, Session] = {}  # session_id → Session
        self._pairing_index: Dict[str, str] = {}  # pairing_code → session_id
        self._unpaired_devices: Set[DeviceConnection] = set()
        self._host = host
        self._port = port
        self._local_ips: list[str] = []

    # ── 设备消息发送 ────────────────────────────────────────────────────────

    @staticmethod
    def _display_width(s: str) -> int:
        """计算字符串在终端中的实际显示宽度（CJK 字符占 2 列）"""
        w = 0
        for ch in s:
            eaw = unicodedata.east_asian_width(ch)
            w += 2 if eaw in ("W", "F") else 1
        return w

    def _pad_center(self, s: str, width: int) -> str:
        """按显示宽度居中填充"""
        sw = self._display_width(s)
        pad = width - sw
        if pad <= 0:
            return s
        left = pad // 2
        return " " * left + s + " " * (pad - left)

    def _print_pairing_banner(self, pairing_code: str, session_id: str) -> None:
        """在 daemon 终端显示醒目的配对码 + QR 码（每个 IP 一个 QR）"""
        hosts = self._local_ips if self._local_ips else ["127.0.0.1"]
        qr_entries: list[tuple[str, str]] = []  # (url, qr_str)
        for host in hosts:
            url = f"http://{host}:{self._port}/pair?code={pairing_code}"
            try:
                from ccbb.qrcode import qr_to_terminal
                qr_str = qr_to_terminal(url)
                qr_entries.append((url, qr_str))
            except Exception:
                qr_entries.append((url, ""))

        spaced_code = "  ".join(pairing_code)
        sid_short = session_id[:8] if len(session_id) > 8 else session_id

        # 自适应边框宽度
        w = 42
        for url, _ in qr_entries:
            w = max(w, self._display_width(url) + 4)

        lines = [
            "",
            "╔" + "═" * w + "╗",
            "║" + self._pad_center(f"Session: {sid_short}", w) + "║",
            "║" + "═" * w + "║",
            "║" + self._pad_center(spaced_code, w) + "║",
            "║" + "═" * w + "║",
        ]
        for i, (url, qr_str) in enumerate(qr_entries):
            if i > 0:
                lines.append("║" + " " * w + "║")
            lines.append("║" + self._pad_center(url, w) + "║")
            if qr_str:
                for line in qr_str.splitlines():
                    lines.append("║" + self._pad_center(line, w) + "║")
        lines += [
            "║" + self._pad_center("在审批设备上输入配对码或扫描二维码", w) + "║",
            "╚" + "═" * w + "╝",
            "",
        ]
        print("\n".join(lines))

    async def _send_to_device(self, device: DeviceConnection, obj: dict) -> None:
        """发送消息到设备"""
        try:
            payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            device.writer.write(payload)
            await device.writer.drain()
        except Exception as e:
            logger.warning(f"发送消息到设备 {device.addr} 失败: {e}")
            self._remove_device(device)

    # ── 会话管理 ────────────────────────────────────────────────────────────

    async def _register_session(self, session_id: str, writer: asyncio.StreamWriter) -> None:
        """注册 CC 会话，返回配对码"""
        if session_id in self._sessions:
            # 会话已存在（resume），返回现有配对码
            session = self._sessions[session_id]
            writer.write(json.dumps({"pairing_code": session.pairing_code}).encode() + b"\n")
            await writer.drain()
            logger.info(f"Session 恢复: {session_id[:8]}... 配对码={session.pairing_code}")
            return

        code = generate_pairing_code()
        while code in self._pairing_index:
            code = generate_pairing_code()

        session = Session(session_id=session_id, pairing_code=code)
        self._sessions[session_id] = session
        self._pairing_index[code] = session_id
        logger.info(f"Session 注册: {session_id[:8]}... 配对码={code}")

        self._print_pairing_banner(code, session_id)

        writer.write(json.dumps({"pairing_code": code}).encode() + b"\n")
        await writer.drain()

    def _unregister_session(self, session_id: str) -> None:
        """清理会话，通知所有配对设备"""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        self._pairing_index.pop(session.pairing_code, None)

        # 通知所有配对设备
        for device in list(session.paired_devices):
            device.session_id = None
            self._unpaired_devices.add(device)
            asyncio.ensure_future(self._send_to_device(device, {
                "type": "session_end", "data": {"session_id": session_id},
            }))
        session.paired_devices.clear()

        # 通知 web 订阅者
        for q in list(session.web_queues):
            try:
                q.put_nowait({"type": "session_end", "data": {"session_id": session_id}})
            except Exception:
                pass
        session.web_queues.clear()

        # 取消所有挂起的请求
        for rid, req in list(session.pending_requests.items()):
            if not req.decision_future.done():
                req.decision_future.set_result("timeout")
        session.pending_requests.clear()

        logger.info(f"Session 结束: {session_id[:8]}...")

    # ── 设备管理 ────────────────────────────────────────────────────────────

    def _remove_device(self, device: DeviceConnection) -> None:
        """移除断开的设备"""
        self._unpaired_devices.discard(device)

        if device.session_id:
            session = self._sessions.get(device.session_id)
            if session:
                session.paired_devices.discard(device)
            device.session_id = None

        try:
            device.writer.close()
        except Exception:
            pass

    async def _handle_pairing_request(self, device: DeviceConnection, pairing_code: str) -> bool:
        """处理设备的配对请求"""
        session_id = self._pairing_index.get(pairing_code)
        if session_id is None:
            await self._send_to_device(device, {
                "type": "pairing_failed", "data": {"reason": "配对码无效或已过期"},
            })
            logger.warning(f"设备 {device.addr} 配对失败，无效配对码: {pairing_code}")
            return False

        session = self._sessions.get(session_id)
        if session is None:
            self._pairing_index.pop(pairing_code, None)
            await self._send_to_device(device, {
                "type": "pairing_failed", "data": {"reason": "配对码已过期"},
            })
            return False

        # 配对（多设备，不踢掉已有设备）
        session.paired_devices.add(device)
        device.session_id = session_id
        self._unpaired_devices.discard(device)

        await self._send_to_device(device, {
            "type": "paired", "data": {"pairing_code": pairing_code, "session_id": session_id},
        })

        # 如果有挂起的请求，推送队首给新设备
        if session.pending_requests:
            first_req = next(iter(session.pending_requests.values()))
            await self._send_to_device(device, {"type": "request", "data": first_req.raw})

        logger.info(f"设备 {device.addr} 配对到 session {session_id[:8]}... "
                     f"(共 {len(session.paired_devices)} 个设备)")
        return True

    # ── 设备连接处理 ────────────────────────────────────────────────────────

    async def _handle_device(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             first_msg: dict) -> None:
        """处理设备连接（持久连接）"""
        addr = writer.get_extra_info("peername")
        logger.info(f"新设备连接: {addr}")

        device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid=str(uuid.uuid4()))
        self._unpaired_devices.add(device)

        try:
            await self._send_to_device(device, {
                "type": "waiting_pairing", "data": {"message": "请输入配对码"},
            })
        except Exception:
            self._remove_device(device)
            return

        await self._process_device_message(device, first_msg)

        try:
            rx_buf = bytearray()
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                rx_buf.extend(data)

                while True:
                    nl = rx_buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(rx_buf[:nl])
                    del rx_buf[:nl + 1]
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception as e:
                        logger.warning(f"设备 {addr} 消息解析失败: {line!r} — {e}")
                        continue
                    logger.debug(f"设备 {addr} → 主机: {json.dumps(msg, ensure_ascii=False)}")
                    await self._process_device_message(device, msg)

        except Exception as e:
            logger.error(f"设备连接处理异常: {e}")
        finally:
            self._remove_device(device)
            logger.info(f"设备断开: {addr}")

    async def _process_device_message(self, device: DeviceConnection, msg: dict) -> None:
        """处理单条设备消息：统一 {type, data} 格式"""
        msg_type = msg.get("type")
        data = msg.get("data", {})
        if msg_type == "decision":
            if device.session_id:
                await self._handle_permission_decision(device, data)
        elif msg_type == "pair":
            pairing_code = data.get("pairing_code")
            if pairing_code:
                await self._handle_pairing_request(device, pairing_code)
        elif msg_type == "hello":
            pass

    def _resolve_decision(self, session_id: str, decision: dict) -> Optional[str]:
        """按请求 ID 查找并 resolve pending request，返回 rid 或 None"""
        session = self._sessions.get(session_id)
        if not session or not session.pending_requests:
            return None

        rid = (decision.get("ccbb_request_id")
               or decision.get("request_id")
               or decision.get("tool_use_id"))
        if rid:
            req = session.pending_requests.get(rid)
        else:
            # 向后兼容：resolve 最早的 pending request
            if session.pending_requests:
                rid = next(iter(session.pending_requests))
                req = session.pending_requests[rid]
            else:
                return None

        if req and not req.decision_future.done():
            req.decision_future.set_result(decision)
            session.pending_requests.pop(rid, None)
            return rid
        return None

    async def _handle_permission_decision(self, device: DeviceConnection, decision: dict) -> None:
        """处理设备审批决策"""
        rid = self._resolve_decision(device.session_id or "", decision)
        if rid:
            logger.info(f"收到决策: {decision} id={rid}")
        else:
            logger.warning(f"收到孤立/过期决策: {decision}")

    # ── Hook 连接处理 ───────────────────────────────────────────────────────

    async def _handle_hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           first_msg: dict) -> None:
        """处理 Hook 连接"""
        session_id = ""
        try:
            action = first_msg.get("action")

            if action == "session_start":
                session_id = first_msg.get("session_id", "")
                if session_id:
                    await self._register_session(session_id, writer)

            elif action == "session_end":
                session_id = first_msg.get("session_id", "")
                if session_id:
                    self._unregister_session(session_id)

            else:
                # PermissionRequest
                session_id = first_msg.get("session_id", "")
                await self._process_permission_request(first_msg, writer, reader)

        except Exception as e:
            logger.error(f"Hook 连接处理异常: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _broadcast(self, session: Session, msg_type: str, data: dict) -> None:
        """统一广播到所有 TCP 设备和 SSE 订阅者"""
        msg = {"type": msg_type, "data": data}
        for dev in list(session.paired_devices):
            try:
                await self._send_to_device(dev, msg)
            except Exception:
                pass
        for q in list(session.web_queues):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    async def _push_next_request(self, session: Session) -> None:
        """推送队列中下一个待处理请求到所有订阅者"""
        if not session.pending_requests:
            return
        rid = next(iter(session.pending_requests))
        req = session.pending_requests[rid]
        logger.info(f"推送下一个请求 id={rid}")
        await self._broadcast(session, "request", req.raw)

    async def _process_permission_request(self, event: dict, writer: asyncio.StreamWriter,
                                           reader: asyncio.StreamReader) -> None:
        """处理审批请求：队首时推送，等待决策后推送下一个"""
        session_id = event.get("session_id", "")
        if not session_id:
            logger.warning("PermissionRequest 缺少 session_id")
            return

        session = self._sessions.get(session_id)
        if session is None:
            # daemon 重启后 session 丢失，自动恢复注册
            code = generate_pairing_code()
            while code in self._pairing_index:
                code = generate_pairing_code()
            session = Session(session_id=session_id, pairing_code=code)
            self._sessions[session_id] = session
            self._pairing_index[code] = session_id
            logger.info(f"Session 自动恢复: {session_id[:8]}... 配对码={code}")
            self._print_pairing_banner(code, session_id)

        rid = str(event.get("tool_use_id") or f"req_{int(time.time() * 1000)}")
        logger.info(f"收到请求 session={session_id[:8]}... id={rid}")

        # 创建待处理请求
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        session.pending_requests[rid] = PendingRequest(id=rid, decision_future=fut, raw=event)

        tool_name = event.get("tool_name") or "?"
        hint = _make_hint(event.get("tool_input"))
        session.entries.insert(0, f"{time.strftime('%H:%M')} {truncate(f'{tool_name}: {hint}', 50)}")
        session.entries = session.entries[:ENTRIES_MAX]

        # 只有队首请求才推送，其余排队等待
        if len(session.pending_requests) == 1:
            logger.info(f"推送请求到设备 id={rid}")
            await self._broadcast(session, "request", event)

        # 等待：设备/web 响应 或 hook 断开（超时由 Claude Code 管理）
        hook_disconnected = False

        async def _watch_hook():
            """安全检测 hook 断开，抑制所有异常"""
            try:
                await reader.read(1)
            except Exception:
                pass

        reader_task = asyncio.ensure_future(_watch_hook())
        fut_task = asyncio.ensure_future(fut)
        done, pending = await asyncio.wait(
            [fut_task, reader_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if fut_task in done:
            result = fut_task.result()
        else:
            result = "closed"
            hook_disconnected = True
            logger.info(f"id={rid} Hook 连接已断开")
        session.pending_requests.pop(rid, None)

        # 广播审批结束到所有订阅者
        behavior = result.get("behavior", "closed") if isinstance(result, dict) else (result or "closed")
        await self._broadcast(session, "done", {"id": rid, "decision": behavior})

        # 推送队列中的下一个请求
        await self._push_next_request(session)

        # 响应 Hook（透传 decision 对象，hook 断开则跳过）
        if not hook_disconnected:
            resp = result if isinstance(result, dict) else {"behavior": result}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
            logger.info(f"id={rid} → {resp.get('behavior', resp)}")

    # ── 连接识别与分发 ──────────────────────────────────────────────────────

    def _is_hook_request(self, msg: dict) -> bool:
        return (
            msg.get("hook_event_name") is not None
            or msg.get("action") in ("session_start", "session_end", "get_pairing_code")
            or "tool_name" in msg
            or "session_id" in msg
        )

    def _is_device_message(self, msg: dict) -> bool:
        return "cmd" in msg

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理客户端连接：通过首条消息识别类型并分发"""
        addr = writer.get_extra_info("peername")
        logger.info(f"新连接: {addr}")

        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{addr}] 60 秒内未收到消息")
            writer.close()
            return

        if not first_line:
            writer.close()
            return

        first_str = first_line.decode("utf-8", errors="replace").strip()

        # HTTP 协议检测
        if first_str.startswith(("GET ", "POST ", "PUT ", "DELETE ", "OPTIONS ", "HEAD ")):
            logger.info(f"[{addr}] 识别为 HTTP 连接")
            from ccbb.web.handler import WebHandler
            handler = WebHandler(self)
            await handler.handle(reader, writer, first_str)
            return

        try:
            msg = json.loads(first_str)
        except Exception as e:
            logger.warning(f"[{addr}] JSON 解析失败: {e}")
            writer.close()
            return

        if self._is_device_message(msg):
            logger.info(f"[{addr}] 识别为设备连接")
            await self._handle_device(reader, writer, first_msg=msg)
        elif self._is_hook_request(msg):
            logger.info(f"[{addr}] 识别为 Hook 连接")
            await self._handle_hook(reader, writer, first_msg=msg)
        else:
            logger.warning(f"[{addr}] 无法识别的消息格式: {msg}")
            writer.close()


# ── 主入口 ─────────────────────────────────────────────────────────────────


async def run() -> None:
    host = os.environ.get("CCBB_TCP_HOST", TCP_HOST_DEFAULT)
    port = int(os.environ.get("CCBB_TCP_PORT", str(TCP_PORT_DEFAULT)))

    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        logger.info("收到退出信号，正在关闭…")
        stop_event.set()

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    bridge = Bridge(host=host, port=port)

    server = await asyncio.start_server(
        bridge.handle_client, host, port,
    )
    logger.info(f"TCP 服务端已启动，监听 {host}:{port}")
    logger.info("  支持多会话多设备配对")

    # 探测本机所有 IP 供二维码使用
    local_ips: list[str] = []
    try:
        hostname = socket.gethostname()
        seen: set[str] = set()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                local_ips.append(ip)
    except Exception:
        pass
    # 补充默认路由 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        default_ip = s.getsockname()[0]
        s.close()
        if default_ip not in local_ips:
            local_ips.insert(0, default_ip)
    except Exception:
        pass
    bridge._local_ips = local_ips
    if local_ips:
        logger.info(f"  本机 IP: {', '.join(local_ips)}")
    else:
        logger.info("  本机 IP: 127.0.0.1 (仅本地)")

    stop_task = asyncio.create_task(stop_event.wait(), name="stop_wait")

    logger.info("claude-code-buddy-bridge 守护进程已就绪 ✓")

    done, pending = await asyncio.wait(
        {stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in pending:
        t.cancel()

    server.close()
    try:
        await server.wait_closed()
    except Exception:
        pass

    logger.info("claude-code-buddy-bridge 已退出")
