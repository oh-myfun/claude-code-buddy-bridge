"""
ccbb.bridge — 守护进程核心

架构说明
--------
支持多个 Claude Code 终端与多个审批设备配对：

  Claude Code CLI ──session_id── 设备
       │                              │
       ▼                              ▼
  TCP Socket (9876)              TCP Socket (9876)
       │                              │
       └──────────── Bridge ────────────┘

配对机制（基于 session_id）：
1. SessionStart hook 触发时，Bridge 注册 session 并生成配对码
2. 配对码 = session_id 前6位（或基于 session_id 生成）
3. 用户在设备上输入配对码并连接
4. Bridge 将设备与 session 配对
5. PermissionRequest hook 使用 session_id 查找配对的设备
6. 设备响应只发送给对应 session 的 Hook

关键设计
--------
1. session_id 作为唯一标识，贯穿整个 session 生命周期
2. 配对码基于 session_id 生成，易于记忆
3. Fail-open：bridge 未运行时，CC 走自己的权限对话框
4. 支持多终端多设备配对

额外改进
--------
- 电脑作为 TCP 服务端，设备主动连接
- 支持完整 Unicode，不再限制中文字符
- 跨平台支持：Windows、macOS、Linux 统一使用 TCP
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Set, Dict

TCP_HOST_DEFAULT = "0.0.0.0"
TCP_PORT_DEFAULT = 9876
PERMISSION_TIMEOUT = 110.0
ENTRIES_MAX = 5

logger = logging.getLogger("ccbb.bridge")


def generate_pairing_code_from_session(session_id: str) -> str:
    """基于 session_id 生成6位配对码"""
    hash_val = hashlib.md5(session_id.encode()).hexdigest()
    num = int(hash_val[:8], 16)
    return str(num % 900000 + 100000)


def truncate(text: str, max_len: int = 60) -> str:
    return text[:max_len]


def _tz_offset_seconds() -> int:
    return -time.altzone if time.daylight and time.localtime().tm_isdst else -time.timezone


@dataclass
class PendingRequest:
    id: str
    tool: str
    hint: str
    decision_future: asyncio.Future
    context: Optional[dict] = None


@dataclass
class SessionInfo:
    """存储 session 信息"""
    session_id: str
    pairing_code: str
    pending_request: Optional[PendingRequest] = None
    entries: list[str] = field(default_factory=list)


@dataclass
class DeviceConnection:
    """表示一个连接的审批设备"""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr: tuple
    uid: str
    pairing_code: Optional[str] = None

    def __hash__(self) -> int:
        return hash(self.uid)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DeviceConnection):
            return self.uid == other.uid
        return False


class Bridge:
    def __init__(self) -> None:
        self._sessions: Dict[str, SessionInfo] = {}
        self._pairings: Dict[str, SessionInfo] = {}
        self._unpaired_devices: Set[DeviceConnection] = set()
        self._pending_decisions: Dict[str, asyncio.Future] = {}

    async def _send_to_device(self, device: DeviceConnection, obj: dict) -> None:
        try:
            payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            device.writer.write(payload)
            await device.writer.drain()
        except Exception as e:
            logger.warning(f"发送消息到设备 {device.addr} 失败: {e}")
            self._remove_device(device)

    def _remove_device(self, device: DeviceConnection) -> None:
        if device in self._unpaired_devices:
            self._unpaired_devices.remove(device)

        for pairing_code, session in list(self._pairings.items()):
            if hasattr(session, 'device') and session.device == device:
                session.device = None
                logger.info(f"设备 {device.addr} 断开，配对 {pairing_code} 解除")

        try:
            device.writer.close()
        except Exception:
            pass

    async def _handle_session_start(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """处理 SessionStart 事件"""
        session_id = msg.get("session_id", "")
        if not session_id:
            logger.warning("SessionStart 缺少 session_id")
            return

        pairing_code = generate_pairing_code_from_session(session_id)
        
        session = SessionInfo(session_id=session_id, pairing_code=pairing_code)
        self._sessions[session_id] = session

        logger.info(f"Session 注册: {session_id[:8]}... 配对码: {pairing_code}")

        writer.write(json.dumps({
            "pairing_code": pairing_code
        }).encode() + b"\n")
        await writer.drain()

    async def _handle_permission_request(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """处理 PermissionRequest 事件"""
        session_id = msg.get("session_id", "")
        if not session_id:
            logger.warning("PermissionRequest 缺少 session_id")
            writer.write(json.dumps({"decision": "timeout"}).encode() + b"\n")
            await writer.drain()
            return

        if session_id not in self._sessions:
            logger.warning(f"未知 session: {session_id[:8]}...")
            writer.write(json.dumps({"decision": "timeout"}).encode() + b"\n")
            await writer.drain()
            return

        session = self._sessions[session_id]
        pairing_code = session.pairing_code

        rid = str(msg.get("id") or f"req_{int(time.time() * 1000)}")
        tool = str(msg.get("tool") or "?")
        hint = str(msg.get("hint") or "")
        context = msg.get("context") if isinstance(msg.get("context"), dict) else None

        logger.info(f"收到请求 session={session_id[:8]}... id={rid} tool={tool}")

        if pairing_code not in self._pairings:
            logger.warning(f"Session {session_id[:8]}... 未配对")
            writer.write(json.dumps({
                "decision": "timeout",
                "error": "设备未配对"
            }).encode() + b"\n")
            await writer.drain()
            return

        session = self._pairings[pairing_code]
        device = getattr(session, 'device', None)
        if not device:
            logger.warning(f"Session {session_id[:8]}... 配对设备已断开")
            writer.write(json.dumps({
                "decision": "timeout",
                "error": "设备已断开"
            }).encode() + b"\n")
            await writer.drain()
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        session.pending_request = PendingRequest(
            id=rid, tool=tool, hint=hint, decision_future=fut, context=context
        )
        session.entries.insert(0, f"{time.strftime('%H:%M')} {truncate(f'{tool}: {hint}', 50)}")
        session.entries = session.entries[:ENTRIES_MAX]

        self._pending_decisions[rid] = fut

        snapshot = {
            "total": 1,
            "running": 0,
            "waiting": 1,
            "msg": truncate(f"approve: {tool}"),
            "entries": list(reversed(session.entries[:ENTRIES_MAX])),
            "tokens": 0,
            "tokens_today": 0,
            "prompt": {
                "id": rid,
                "tool": truncate(tool),
                "hint": truncate(hint),
            },
        }
        if context:
            snapshot["context"] = context

        try:
            await self._send_to_device(device, snapshot)
        except Exception as e:
            logger.warning(f"发送快照失败: {e}")
            writer.write(json.dumps({"decision": "timeout"}).encode() + b"\n")
            await writer.drain()
            return

        try:
            decision = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            decision = "timeout"
            logger.warning(f"id={rid} 审批超时")

        session.pending_request = None
        if rid in self._pending_decisions:
            del self._pending_decisions[rid]

        writer.write(json.dumps({"decision": decision}).encode() + b"\n")
        await writer.drain()

        logger.info(f"id={rid} → decision={decision}")

    async def _handle_pairing_request(self, device: DeviceConnection, pairing_code: str) -> bool:
        """处理设备的配对请求"""
        for session_id, session in self._sessions.items():
            if session.pairing_code == pairing_code:
                self._pairings[pairing_code] = session
                session.device = device
                device.pairing_code = pairing_code
                self._unpaired_devices.discard(device)
                
                logger.info(f"配对成功: {pairing_code} <-> {session_id[:8]}...")

                await self._send_to_device(device, {
                    "cmd": "paired",
                    "pairing_code": pairing_code,
                    "session_id": session_id[:8] + "..."
                })

                if session.pending_request:
                    await self._send_to_device(device, {
                        "time": [int(time.time()), _tz_offset_seconds()]
                    })

                return True

        await self._send_to_device(device, {
            "cmd": "pairing_failed",
            "reason": "配对码无效或已过期"
        })
        logger.warning(f"设备 {device.addr} 配对失败，无效配对码: {pairing_code}")
        return False

    async def _handle_permission_decision(self, device: DeviceConnection, msg: dict) -> None:
        """处理设备的审批决策"""
        mid = msg.get("id")
        decision = msg.get("decision")

        if mid in self._pending_decisions:
            fut = self._pending_decisions[mid]
            if not fut.done():
                fut.set_result(decision)
                logger.info(f"收到决策 id={mid} decision={decision}")

            try:
                await self._send_to_device(device, {"ack": "permission", "ok": True, "n": 0})
            except Exception as e:
                logger.warning("permission ack 发送失败: %s", e)
        else:
            logger.warning(f"收到孤立 permission id={mid!r}")

    async def _handle_device(self, first_msg: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理设备连接"""
        addr = writer.get_extra_info("peername")
        logger.info(f"新设备连接: {addr}")

        device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid=str(uuid.uuid4()))
        self._unpaired_devices.add(device)

        try:
            await self._send_to_device(device, {
                "cmd": "waiting_pairing",
                "message": "请输入配对码"
            })
        except Exception as e:
            logger.warning(f"发送初始消息失败: {e}")
            self._remove_device(device)
            return

        try:
            rx_buf = bytearray()
            
            async def process_msg(msg: dict):
                cmd = msg.get("cmd")
                if cmd == "pair":
                    pairing_code = msg.get("pairing_code")
                    if pairing_code:
                        await self._handle_pairing_request(device, pairing_code)
                elif cmd == "permission":
                    if device.pairing_code:
                        await self._handle_permission_decision(device, msg)
                    else:
                        await self._send_to_device(device, {
                            "cmd": "error",
                            "reason": "请先配对"
                        })
                else:
                    logger.warning(f"未知设备命令: {cmd}")

            await process_msg(first_msg)

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
                    await process_msg(msg)

        except Exception as e:
            logger.error(f"设备连接处理异常: {e}")
        finally:
            self._remove_device(device)
            logger.info(f"设备断开: {addr}")

    async def _handle_hook(self, msg: dict, writer: asyncio.StreamWriter):
        """处理 Hook 连接（单次请求）"""
        try:
            logger.debug(f"Hook 消息: {json.dumps(msg, ensure_ascii=False)[:200]}")

            action = msg.get("action", "")
            
            if action == "session_start":
                await self._handle_session_start(msg, writer)
            elif "session_id" in msg and "tool" in msg:
                await self._handle_permission_request(msg, writer)
            else:
                logger.warning(f"未知的 Hook 消息格式: {msg}")

        except Exception as e:
            logger.error(f"Hook 连接处理异常: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _is_hook_request(self, msg: dict) -> bool:
        return "action" in msg or "session_id" in msg

    def _is_device_message(self, msg: dict) -> bool:
        return "cmd" in msg and msg["cmd"] in ["pair", "permission"]

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername")
        logger.info(f"新连接: {addr}")

        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{addr}] 5 秒内未收到消息")
            writer.close()
            return

        if not first_line:
            writer.close()
            return

        try:
            msg = json.loads(first_line.decode("utf-8"))
        except Exception as e:
            logger.warning(f"[{addr}] JSON 解析失败: {e}")
            writer.close()
            return

        if self._is_hook_request(msg):
            logger.info(f"[{addr}] 识别为 Hook 连接")
            await self._handle_hook(msg, writer)
        elif self._is_device_message(msg):
            logger.info(f"[{addr}] 识别为设备连接")
            await self._handle_device(msg, reader, writer)
        else:
            logger.warning(f"[{addr}] 无法识别的消息格式: {msg}")
            writer.close()


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

    bridge = Bridge()

    server = await asyncio.start_server(
        bridge.handle_client, host, port
    )
    logger.info(f"TCP 服务端已启动，监听 {host}:{port}")
    logger.info("  - 支持多终端多设备配对（基于 session_id）")

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
