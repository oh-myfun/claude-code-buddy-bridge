"""
ccbb.bridge — 守护进程核心

架构说明
--------
支持多个 Claude Code 会话与多个审批设备配对：

  Claude Code 会话 A ──pairing_code_A── 设备 A
  Claude Code 会话 B ──pairing_code_B── 设备 B

配对机制：
1. SessionStart hook 触发时，Bridge 注册会话并从 session_id 派生 8 位配对码
2. Bridge 在 daemon 终端显示配对码
3. 用户在设备上输入配对码完成配对（支持设备先连接，CC 后启动的预配对）
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
import signal
import socket
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Optional, Set, Dict

# ── 常量 ───────────────────────────────────────────────────────────────────
TCP_HOST_DEFAULT = "0.0.0.0"
TCP_PORT_DEFAULT = 9876

logger = logging.getLogger("ccbb.bridge")


# ── 工具函数 ────────────────────────────────────────────────────────────────


def derive_pairing_code(session_id: str) -> str:
    """从 session_id 前 8 位派生配对码，同一会话始终相同"""
    return session_id[:8].upper()


# ── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class PendingRequest:
    id: str
    decision_future: asyncio.Future
    raw: dict  # hook 发来的完整请求，透传给设备


@dataclass
class Session:
    """一个 Claude Code 会话"""
    session_id: str
    pairing_code: str
    paired_devices: Set["DeviceConnection"] = field(default_factory=set)
    pending_requests: Dict[str, PendingRequest] = field(default_factory=dict)
    head_pushed: bool = False


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
        self._pending_pairings: Dict[str, Set[DeviceConnection]] = {}  # pairing_code → 等待该会话的设备
        self._unpaired_devices: Set[DeviceConnection] = set()
        self._host = host
        self._port = port

    # ── UDP 发现 ────────────────────────────────────────────────────────────

    def get_discovery_info(self, peer_addr: tuple) -> dict:
        """构建 UDP 发现响应"""
        sessions = [
            {"session_id": sid, "pairing_code": s.pairing_code}
            for sid, s in self._sessions.items()
        ]
        return {
            "type": "discover_response",
            "host": self._get_local_ip(peer_addr[0]),
            "port": self._port,
            "sessions": sessions,
        }

    @staticmethod
    def _get_local_ip(peer_ip: str) -> str:
        """获取与 peer_ip 同网段的本机 IP"""
        try:
            # 用 UDP 连接目标 IP 来确定出口地址（不实际发包）
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((peer_ip, 1))
                return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"

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
        """在 daemon 终端显示醒目的配对码"""
        spaced_code = "  ".join(pairing_code)
        sid_short = session_id[:8] if len(session_id) > 8 else session_id

        w = 42
        lines = [
            "",
            "╔" + "═" * w + "╗",
            "║" + self._pad_center(f"Session: {sid_short}", w) + "║",
            "║" + "═" * w + "║",
            "║" + self._pad_center(spaced_code, w) + "║",
            "║" + "═" * w + "║",
            "║" + self._pad_center("在审批设备上输入配对码", w) + "║",
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
            logger.warning(f"[设备] 发送到 {device.addr} 失败: {e}")
            self._remove_device(device)

    # ── 会话管理 ────────────────────────────────────────────────────────────

    async def _register_session(self, session_id: str, writer: asyncio.StreamWriter) -> None:
        """注册 CC 会话，返回配对码"""
        if session_id in self._sessions:
            # 会话已存在（resume），返回现有配对码
            session = self._sessions[session_id]
            writer.write(json.dumps({"pairing_code": session.pairing_code}).encode() + b"\n")
            await writer.drain()
            logger.info(f"[Session] 恢复 {session_id[:8]}... 配对码={session.pairing_code}")
            return

        code = derive_pairing_code(session_id)

        session = Session(session_id=session_id, pairing_code=code)
        self._sessions[session_id] = session
        self._pairing_index[code] = session_id
        logger.info(f"[Session] 注册 {session_id[:8]}... 配对码={code}")

        self._print_pairing_banner(code, session_id)

        writer.write(json.dumps({"pairing_code": code}).encode() + b"\n")
        await writer.drain()

        await self._auto_pair_pending(session)

    async def _auto_pair_pending(self, session: Session) -> None:
        """自动匹配预配对设备到新创建/恢复的 session"""
        pending = self._pending_pairings.pop(session.pairing_code, None)
        if not pending:
            return
        for dev in list(pending):
            self._unpaired_devices.discard(dev)
            session.paired_devices.add(dev)
            dev.session_id = session.session_id
            await self._send_to_device(dev, {
                "type": "paired", "data": {"pairing_code": session.pairing_code, "session_id": session.session_id},
            })
            # 推送队首请求（如果有）
            if session.pending_requests and not session.head_pushed and dev in session.paired_devices:
                first_req = next(iter(session.pending_requests.values()))
                session.head_pushed = True
                await self._send_to_device(dev, {"type": "request", "data": {**first_req.raw, "ccbb_request_id": first_req.id}})
                # 推送失败时 _send_to_device 会调 _remove_device，设备已不在 session 中
                if dev not in session.paired_devices:
                    session.head_pushed = False
        logger.info(f"[Session] {session.session_id[:8]}... 自动配对 {len(pending)} 个预配对设备")

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

        # 取消所有挂起的请求
        for rid, req in list(session.pending_requests.items()):
            if not req.decision_future.done():
                req.decision_future.set_result("timeout")
        session.pending_requests.clear()

        logger.info(f"[Session] 结束 {session_id[:8]}...")

    # ── 设备管理 ────────────────────────────────────────────────────────────

    def _remove_device(self, device: DeviceConnection) -> None:
        """移除断开的设备"""
        self._unpaired_devices.discard(device)

        # 从预配对集合中移除
        for pending_set in self._pending_pairings.values():
            pending_set.discard(device)

        if device.session_id:
            session = self._sessions.get(device.session_id)
            if session:
                session.paired_devices.discard(device)
                # 所有配对设备都已断开，清理挂起的请求
                if not session.paired_devices:
                    for rid, req in list(session.pending_requests.items()):
                        if not req.decision_future.done():
                            req.decision_future.set_result("closed")
            device.session_id = None

        try:
            device.writer.close()
        except Exception:
            pass

    async def _handle_pairing_request(self, device: DeviceConnection, pairing_code: str) -> bool:
        """处理设备的配对请求，支持 session 未启动时的预配对"""
        session_id = self._pairing_index.get(pairing_code.upper())
        if session_id is None:
            # session 尚未启动，存为预配对
            code_upper = pairing_code.upper()
            pending_set = self._pending_pairings.setdefault(code_upper, set())
            pending_set.add(device)
            await self._send_to_device(device, {
                "type": "pairing_pending", "data": {"pairing_code": code_upper, "message": "等待会话启动"},
            })
            logger.info(f"[设备] {device.addr} 预配对，等待 session {code_upper}...")
            return True

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

        # 如果有挂起的请求且尚未推送，推送队首给新设备
        if session.pending_requests and not session.head_pushed:
            first_req = next(iter(session.pending_requests.values()))
            session.head_pushed = True
            await self._send_to_device(device, {"type": "request", "data": {**first_req.raw, "ccbb_request_id": first_req.id}})

        logger.info(f"[设备] {device.addr} 配对到 session {session_id[:8]}... "
                     f"(共 {len(session.paired_devices)} 个设备)")
        return True

    # ── 设备连接处理 ────────────────────────────────────────────────────────

    async def _handle_device(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             first_msg: dict) -> None:
        """处理设备连接（持久连接）"""
        addr = writer.get_extra_info("peername")
        self._set_keepalive(writer)

        device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid=str(uuid.uuid4()))
        self._unpaired_devices.add(device)

        try:
            await self._send_to_device(device, {
                "type": "waiting_pairing", "data": {"message": "请输入配对码"},
            })
        except Exception:
            self._remove_device(device)
            return

        try:
            await self._process_device_message(device, first_msg)

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
                        logger.warning(f"[设备] {addr} 消息解析失败: {line!r} — {e}")
                        continue
                    logger.debug(f"[设备] {addr} 收到: {json.dumps(msg, ensure_ascii=False)}")
                    await self._process_device_message(device, msg)

        except Exception as e:
            logger.error(f"[设备] {addr} 异常: {e}")
        finally:
            self._remove_device(device)
            logger.info(f"[设备] 断开 {addr}")

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
               or decision.get("request_id"))
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
            return rid
        return None

    async def _handle_permission_decision(self, device: DeviceConnection, decision: dict) -> None:
        """处理设备审批决策"""
        rid = self._resolve_decision(device.session_id or "", decision)
        if rid:
            logger.info(f"[审批] 收到决策 id={rid} behavior={decision.get('behavior')}")
        else:
            logger.warning(f"[审批] 孤立/过期决策: {decision}")

    # ── Hook 连接处理 ───────────────────────────────────────────────────────

    @staticmethod
    def _set_keepalive(writer: asyncio.StreamWriter, idle: int = 5, interval: int = 3) -> None:
        """设置 TCP keepalive，快速检测对端断开"""
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
            elif hasattr(socket, "SIO_KEEPALIVE_VALS"):
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        except (OSError, AttributeError):
            pass

    async def _handle_hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                           first_msg: dict) -> None:
        """处理 Hook 连接"""
        self._set_keepalive(writer)
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
                return

            elif action == "status":
                session_id = first_msg.get("session_id", "")
                status = first_msg.get("status", {})
                if session_id and status:
                    self._broadcast_status(session_id, status)
                return

            else:
                # PermissionRequest
                session_id = first_msg.get("session_id", "")
                await self._process_permission_request(first_msg, writer, reader)

        except Exception as e:
            logger.error(f"[Hook] 异常: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _broadcast(self, session: Session, msg_type: str, data: dict) -> None:
        """广播到所有配对设备"""
        msg = {"type": msg_type, "data": data}
        for dev in list(session.paired_devices):
            try:
                await self._send_to_device(dev, msg)
            except Exception:
                pass

    def _broadcast_status(self, session_id: str, status: dict) -> None:
        """广播 CC 状态变化到配对设备（同步 fire-and-forget）"""
        session = self._sessions.get(session_id)
        if not session or not session.paired_devices:
            return
        state = status.get("state", "?")
        asyncio.ensure_future(self._broadcast(session, "status", status))
        logger.info(f"[状态] session={session_id[:8]}... state={state}")

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
            code = derive_pairing_code(session_id)
            session = Session(session_id=session_id, pairing_code=code)
            self._sessions[session_id] = session
            self._pairing_index[code] = session_id
            logger.info(f"[Session] 自动恢复 {session_id[:8]}... 配对码={code}")
            self._print_pairing_banner(code, session_id)
            await self._auto_pair_pending(session)

        rid = f"req_{uuid.uuid4().hex[:12]}"
        logger.info(f"[审批] 收到请求 session={session_id[:8]}... id={rid}")

        # 创建待处理请求
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        session.pending_requests[rid] = PendingRequest(id=rid, decision_future=fut, raw=event)

        # 队首且尚未推送时才推送
        if not session.head_pushed:
            session.head_pushed = True
            logger.info(f"[审批] 推送请求 id={rid}")
            await self._broadcast(session, "request", {**event, "ccbb_request_id": rid})

        # 等待：设备响应 或 hook 断开（超时由 hook.py 管理）
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
            logger.info(f"[Hook] id={rid} 连接已断开")
        session.pending_requests.pop(rid, None)

        # 广播审批结束到所有订阅者
        behavior = result.get("behavior", "closed") if isinstance(result, dict) else (result or "closed")
        await self._broadcast(session, "done", {"id": rid, "decision": behavior})

        # 推送队列中下一个请求（同步重置标志后再 await，防止竞态）
        session.head_pushed = False
        if session.pending_requests:
            next_req = next(iter(session.pending_requests.values()))
            session.head_pushed = True
            logger.info(f"[审批] 推送下一个请求 id={next_req.id}")
            await self._broadcast(session, "request", {**next_req.raw, "ccbb_request_id": next_req.id})

        # 响应 Hook（透传 decision 对象，hook 断开则跳过）
        if not hook_disconnected:
            resp = result if isinstance(result, dict) else {"behavior": result}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
            logger.info(f"[Hook] id={rid} 响应 {resp.get('behavior', resp)}")

    # ── 连接识别与分发 ──────────────────────────────────────────────────────

    def _is_hook_request(self, msg: dict) -> bool:
        return (
            msg.get("hook_event_name") is not None
            or msg.get("action") in ("session_start", "session_end", "status")
            or "tool_name" in msg
        )

    def _is_device_message(self, msg: dict) -> bool:
        return msg.get("type") in ("hello", "pair", "decision")

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理客户端连接：通过首条消息识别类型并分发"""
        addr = writer.get_extra_info("peername")

        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(f"[连接] {addr} 60 秒内未收到消息")
            writer.close()
            return

        if not first_line:
            writer.close()
            return

        first_str = first_line.decode("utf-8", errors="replace").strip()

        try:
            msg = json.loads(first_str)
        except Exception as e:
            logger.warning(f"[连接] {addr} JSON 解析失败: {e}")
            writer.close()
            return

        if self._is_device_message(msg):
            logger.info(f"[设备] 连接 {addr}")
            await self._handle_device(reader, writer, first_msg=msg)
        elif self._is_hook_request(msg):
            logger.info(f"[Hook] {addr} 连接 action={msg.get('action', 'PermissionRequest')}")
            await self._handle_hook(reader, writer, first_msg=msg)
        else:
            logger.warning(f"[连接] {addr} 无法识别的消息格式: {msg}")
            writer.close()


# ── UDP 发现协议 ───────────────────────────────────────────────────────────


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP 广播发现：设备发送 {"type":"discover"}，daemon 回复会话列表"""

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if msg.get("type") != "discover":
            return
        resp = self._bridge.get_discovery_info(addr)
        if self._transport:
            self._transport.sendto(
                (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"),
                addr,
            )
            logger.info(f"[发现] 回复 {addr[0]}:{addr[1]}，{len(resp['sessions'])} 个会话")


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

    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(bridge),
        local_addr=(host, port),
    )
    logger.info(f"UDP 发现服务已启动，监听 {host}:{port}")

    stop_task = asyncio.create_task(stop_event.wait(), name="stop_wait")

    logger.info("claude-code-buddy-bridge 守护进程已就绪 ✓")

    done, pending = await asyncio.wait(
        {stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in pending:
        t.cancel()

    server.close()
    udp_transport.close()
    try:
        await server.wait_closed()
    except Exception:
        pass

    logger.info("claude-code-buddy-bridge 已退出")
