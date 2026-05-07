"""
ccbb.bridge — 守护进程核心

架构说明
--------
外部有两条路径同时运行：

  Claude Code CLI
      │ PreToolUse hook（每次工具调用触发）
      ▼
  TCP Socket  (localhost:HOOK_PORT) 或 Unix Socket (/tmp/ccbb.sock)
      │
      ▼
  Bridge（本模块 - TCP 服务端）◄─── TCP ─── 设备（TCP 客户端）
      │
      ▼
  把 decision 写回 hook 进程 → hook 输出 CC 协议 JSON → Claude Code 继续

关键设计（借鉴 CharmYue/cc-buddy-bridge）
-----------------------------------------
1. permission_lock 串行化并发请求，第二个请求在第一个审批完成后才弹出。
2. EOF 竞争检测：若 hook 进程提前退出（用户 Esc / CC 超时），立即清空设备
   显示，而不是傻等 PERMISSION_TIMEOUT。
3. 心跳发送到所有连接的设备。
4. Fail-open：bridge 未运行时，CC 走自己的权限对话框。

额外改进
--------
- 电脑作为 TCP 服务端，设备主动连接
- 支持多个设备可同时连接
- 支持完整 Unicode，不再限制中文字符
- 跨平台支持：Windows (TCP) 和 Unix (Unix Socket/TCP)
- 通过环境变量 CCBB_TCP_HOST / CCBB_TCP_PORT 配置设备连接端口
- 通过环境变量 CCBB_HOOK_PORT 配置 hook 通信端口（Windows 使用 TCP）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Set

# ── 跨平台常量 ───────────────────────────────────────────────────────────────────
# 统一使用 TCP Socket，跨平台完全一致，性能足够（本地通信）

TCP_HOST_DEFAULT = "0.0.0.0"  # 监听所有网络接口
TCP_PORT_DEFAULT = 9876  # 设备连接端口
HOOK_PORT_DEFAULT = 9877  # hook 连接端口（仅本地访问）
HEARTBEAT_INTERVAL = 3.0  # 秒
PERMISSION_TIMEOUT = 110.0  # 秒，必须小于 CC hook 超时（120s）
ENTRIES_MAX = 5  # 设备显示的历史记录上限

logger = logging.getLogger("ccbb.bridge")


# ── 文本截断（保护设备显示）───────────────────────────────────────────────


def truncate(text: str, max_len: int = 60) -> str:
    """截断文本，保护设备显示。"""
    return text[:max_len]


# ── 时区偏移（供设备时钟同步）────────────────────────────────────────────────
def _tz_offset_seconds() -> int:
    return -time.altzone if time.daylight and time.localtime().tm_isdst else -time.timezone


# ── 数据结构 ──────────────────────────────────────────────────────────────────
@dataclass
class PendingRequest:
    id: str
    tool: str
    hint: str
    decision_future: asyncio.Future
    context: Optional[dict] = None


@dataclass
class DeviceConnection:
    """表示一个连接的设备"""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr: tuple


@dataclass
class BridgeState:
    """所有可观测状态集中在一处，方便序列化为 TCP 快照。"""
    pending: Optional[PendingRequest] = None
    entries: list[str] = field(default_factory=list)
    devices: Set[DeviceConnection] = field(default_factory=set)

    def snapshot(self) -> dict:
        """生成发给设备的标准快照 payload。"""
        if self.pending is not None:
            snapshot = {
                "total": 1,
                "running": 0,
                "waiting": 1,
                "msg": truncate(f"approve: {self.pending.tool}"),
                # 设备期望最旧在前 → reversed
                "entries": list(reversed(self.entries[:ENTRIES_MAX])),
                "tokens": 0,
                "tokens_today": 0,
                "prompt": {
                    "id": self.pending.id,
                    "tool": truncate(self.pending.tool),
                    "hint": truncate(self.pending.hint),
                },
            }
            if self.pending.context:
                snapshot["context"] = self.pending.context
            return snapshot
        return {
            "total": 0,
            "running": 0,
            "waiting": 0,
            "msg": "",
            "entries": list(reversed(self.entries[:ENTRIES_MAX])),
            "tokens": 0,
            "tokens_today": 0,
        }

    def push_entry(self, text: str) -> None:
        ts = time.strftime("%H:%M")
        self.entries.insert(0, f"{ts} {truncate(text, 50)}")
        self.entries = self.entries[:ENTRIES_MAX]


# ── Bridge 主类 ───────────────────────────────────────────────────────────────
class Bridge:
    def __init__(self) -> None:
        self.state = BridgeState()
        self._permission_lock = asyncio.Lock()

    async def send_to_device(self, device: DeviceConnection, obj: dict) -> None:
        """发送消息到单个设备"""
        try:
            payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            device.writer.write(payload)
            await device.writer.drain()
        except Exception as e:
            logger.warning(f"发送消息到设备 {device.addr} 失败: {e}")
            self._remove_device(device)

    async def broadcast(self, obj: dict) -> None:
        """广播消息到所有连接的设备"""
        disconnected = []
        for device in list(self.state.devices):
            try:
                payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
                device.writer.write(payload)
                await device.writer.drain()
            except Exception as e:
                logger.warning(f"广播消息到设备 {device.addr} 失败: {e}")
                disconnected.append(device)

        for device in disconnected:
            self._remove_device(device)

    async def push_snapshot(self) -> None:
        """推送快照到所有设备"""
        await self.broadcast(self.state.snapshot())

    def _remove_device(self, device: DeviceConnection) -> None:
        """移除断开的设备"""
        if device in self.state.devices:
            self.state.devices.remove(device)
            try:
                device.writer.close()
            except Exception:
                pass
            logger.info(f"设备断开连接: {device.addr}")

    async def handle_device_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理新设备连接"""
        addr = writer.get_extra_info("peername")
        logger.info(f"新设备连接: {addr}")

        device = DeviceConnection(reader=reader, writer=writer, addr=addr)
        self.state.devices.add(device)

        # 发送时间同步
        try:
            await self.send_to_device(device, {"time": [int(time.time()), _tz_offset_seconds()]})
            # 发送当前状态
            await self.send_to_device(device, self.state.snapshot())
        except Exception as e:
            logger.warning(f"发送初始消息到设备 {addr} 失败: {e}")
            self._remove_device(device)
            return

        # 持续接收设备消息
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

                    if msg.get("cmd") == "permission":
                        await self._handle_permission_decision(msg)

        except Exception as e:
            logger.error(f"设备连接处理异常: {e}")
        finally:
            self._remove_device(device)

    async def _handle_permission_decision(self, msg: dict) -> None:
        """处理设备的审批决策"""
        mid = msg.get("id")
        decision = msg.get("decision")

        # 发送确认到设备
        try:
            await self.broadcast({"ack": "permission", "ok": True, "n": 0})
        except Exception as e:
            logger.warning("permission ack 广播失败: %s", e)

        pending = self.state.pending
        if pending and pending.id == mid and not pending.decision_future.done():
            pending.decision_future.set_result(decision)
        else:
            logger.warning(f"收到孤立 permission id={mid!r} decision={decision!r}")

    async def heartbeat_loop(self) -> None:
        """定期广播心跳/快照"""
        while True:
            try:
                await self.push_snapshot()
            except Exception as e:
                logger.warning(f"心跳失败: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── Hook 客户端处理 ────────────────────────────────────────────────────────

    async def handle_hook_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        处理来自 hook.py 的单次连接。

        时序：
          1. 读取请求 JSON（含 id / tool / hint）
          2. 获取 permission_lock（串行化并发请求）
          3. 推送快照给所有设备，设备 UI 亮起
          4. 同时等待：(a) 设备按键决策 或 (b) hook 进程 socket EOF
             — 先到先得，避免 hook 被 CC 提前 kill 后设备一直亮着
          5. 把 decision 写回 hook 进程
        """
        peer = writer.get_extra_info("peername") or "?"
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[{peer}] 5 秒内未收到请求行")
            writer.close()
            return

        if not raw:
            writer.close()
            return

        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.warning(f"[{peer}] JSON 解析失败: {e} raw={raw!r}")
            writer.write((json.dumps({"decision": "error", "error": "bad_json"}) + "\n").encode())
            await writer.drain()
            writer.close()
            return

        rid = str(req.get("id") or f"req_{int(time.time() * 1000)}")
        tool = str(req.get("tool") or "?")
        hint = str(req.get("hint") or "")
        context = req.get("context") if isinstance(req.get("context"), dict) else None

        logger.info(f"收到请求 id={rid} tool={tool} hint={hint!r}")

        async with self._permission_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.state.pending = PendingRequest(id=rid, tool=tool, hint=hint, decision_future=fut, context=context)
            self.state.push_entry(f"{tool}: {hint}")

            try:
                await self.push_snapshot()
            except Exception as e:
                logger.warning("快照推送失败: %s", e)

            # 竞争：设备决策 vs hook 进程提前退出（EOF）
            decision_task = asyncio.create_task(
                asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT),
                name=f"decision:{rid}",
            )
            eof_task = asyncio.create_task(reader.read(1), name=f"eof:{rid}")

            done, _ = await asyncio.wait(
                {decision_task, eof_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            client_gone = False

            if decision_task in done:
                eof_task.cancel()
                try:
                    decision = decision_task.result()
                except asyncio.TimeoutError:
                    decision = "timeout"
                    logger.warning(f"id={rid} 审批超时，默认拒绝")
            else:
                # hook 进程先退出 → 立即清空设备显示
                client_gone = True
                decision = "abandoned"
                decision_task.cancel()
                logger.info(f"id={rid} hook 进程已离开，清空设备显示")

            self.state.pending = None

            try:
                await self.push_snapshot()
            except Exception as e:
                logger.warning("清空快照推送失败: %s", e)

        logger.info(f"id={rid} → decision={decision}")

        if not client_gone:
            try:
                writer.write((json.dumps({"decision": decision}) + "\n").encode())
                await writer.drain()
            except Exception as e:
                logger.warning("写回决策失败: %s", e)

        try:
            writer.close()
        except Exception:
            pass


# ── 主入口 ─────────────────────────────────────────────────────────────────────


async def run() -> None:
    host = os.environ.get("CCBB_TCP_HOST", TCP_HOST_DEFAULT)
    device_port = int(os.environ.get("CCBB_TCP_PORT", str(TCP_PORT_DEFAULT)))
    hook_port = int(os.environ.get("CCBB_HOOK_PORT", str(HOOK_PORT_DEFAULT)))

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

    # 启动 TCP 服务端（监听设备连接）
    device_server = await asyncio.start_server(
        bridge.handle_device_connection, host, device_port
    )
    logger.info(f"TCP 服务端已启动，设备连接监听 {host}:{device_port}")

    # 启动 TCP 服务端（监听 hook 连接）
    hook_server = await asyncio.start_server(
        bridge.handle_hook_client, "127.0.0.1", hook_port
    )
    logger.info(f"TCP 服务端已启动，hook 通信监听 127.0.0.1:{hook_port}")

    hb_task = asyncio.create_task(bridge.heartbeat_loop(), name="heartbeat")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop_wait")

    logger.info("claude-code-buddy-bridge 守护进程已就绪 ✓")

    done, pending = await asyncio.wait(
        {hb_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in pending:
        t.cancel()

    for t in done:
        if t is not stop_task and not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(f"任务 {t.get_name()} 异常退出: {exc!r}")

    # 清理所有设备连接
    for device in list(bridge.state.devices):
        try:
            device.writer.close()
            await device.writer.wait_closed()
        except Exception:
            pass

    hook_server.close()
    try:
        await hook_server.wait_closed()
    except Exception:
        pass

    device_server.close()
    try:
        await device_server.wait_closed()
    except Exception:
        pass

    logger.info("claude-code-buddy-bridge 已退出")
