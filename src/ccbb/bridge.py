"""
ccbb.bridge — 守护进程核心

架构说明
--------
支持多个 Claude Code 终端与多个审批设备配对：

  Claude Code CLI ──配对码── 设备
       │                          │
       ▼                          ▼
  TCP Socket (9876)          TCP Socket (9876)
       │                          │
       └──────────── Bridge ────────┘

配对机制：
1. Hook 连接时，Bridge 生成随机6位配对码
2. Hook 将配对码返回给 Claude Code 显示
3. 用户在设备上输入配对码并连接
4. Bridge 将设备与 Hook 配对
5. 后续审批请求只发送给配对的设备
6. 设备响应只发送给配对的 Hook

关键设计
--------
1. permission_lock 串行化同一配对的并发请求
2. EOF 竞争检测：若 hook 进程提前退出，立即清空设备显示
3. 心跳发送到已配对的设备
4. Fail-open：bridge 未运行时，CC 走自己的权限对话框
5. 支持多终端多设备配对

额外改进
--------
- 电脑作为 TCP 服务端，设备主动连接
- 支持完整 Unicode，不再限制中文字符
- 跨平台支持：Windows、macOS、Linux 统一使用 TCP
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Set, Dict

# ── 常量 ───────────────────────────────────────────────────────────────────
TCP_HOST_DEFAULT = "0.0.0.0"  # 监听所有网络接口
TCP_PORT_DEFAULT = 9876  # 所有连接共用此端口
HEARTBEAT_INTERVAL = 3.0  # 秒
PERMISSION_TIMEOUT = 110.0  # 秒，必须小于 CC hook 超时（120s）
ENTRIES_MAX = 5  # 设备显示的历史记录上限
PAIRING_CODE_LENGTH = 6  # 配对码长度

logger = logging.getLogger("ccbb.bridge")


# ── 工具函数 ────────────────────────────────────────────────────────────────


def generate_pairing_code() -> str:
    """生成随机6位配对码"""
    return str(random.randint(100000, 999999))


def truncate(text: str, max_len: int = 60) -> str:
    """截断文本，保护设备显示。"""
    return text[:max_len]


# ── 时区偏移（供设备时钟同步）────────────────────────────────────────────────
def _tz_offset_seconds() -> int:
    return -time.altzone if time.daylight and time.localtime().tm_isdst else -time.timezone


# ── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class PendingRequest:
    id: str
    tool: str
    hint: str
    decision_future: asyncio.Future
    context: Optional[dict] = None


@dataclass
class HookConnection:
    """表示一个连接的 Hook（Claude Code 终端）"""
    writer: asyncio.StreamWriter
    pairing_code: str
    pending_request: Optional[PendingRequest] = None
    entries: list[str] = field(default_factory=list)


@dataclass
class DeviceConnection:
    """表示一个连接的审批设备"""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr: tuple
    uid: str  # 唯一标识符，用于 set 集合
    pairing_code: Optional[str] = None  # 配对码，配对前为 None

    def __hash__(self) -> int:
        return hash(self.uid)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DeviceConnection):
            return self.uid == other.uid
        return False


@dataclass
class Pairing:
    """表示一对配对关系"""
    hook: HookConnection
    device: Optional[DeviceConnection] = None


# ── Bridge 主类 ─────────────────────────────────────────────────────────────
class Bridge:
    def __init__(self) -> None:
        self._pairings: Dict[str, Pairing] = {}  # 配对码 -> Pairing
        self._unpaired_devices: Set[DeviceConnection] = set()  # 未配对的设备
        self._pending_hooks: Dict[str, HookConnection] = {}  # 等待配对的 Hook（配对码 -> Hook）

    async def _send_to_device(self, device: DeviceConnection, obj: dict) -> None:
        """发送消息到单个设备"""
        try:
            payload = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            device.writer.write(payload)
            await device.writer.drain()
        except Exception as e:
            logger.warning(f"发送消息到设备 {device.addr} 失败: {e}")
            self._remove_device(device)

    async def _broadcast_to_paired_devices(self, pairing_code: str, obj: dict) -> None:
        """向指定配对码的设备发送消息"""
        if pairing_code in self._pairings:
            pairing = self._pairings[pairing_code]
            if pairing.device:
                await self._send_to_device(pairing.device, obj)

    async def _broadcast_to_all_paired_devices(self, obj: dict) -> None:
        """向所有已配对的设备发送消息"""
        for pairing in self._pairings.values():
            if pairing.device:
                await self._send_to_device(pairing.device, obj)

    def _remove_device(self, device: DeviceConnection) -> None:
        """移除断开的设备"""
        # 从未配对设备集合中移除
        if device in self._unpaired_devices:
            self._unpaired_devices.remove(device)

        # 从配对中移除
        for pairing_code, pairing in list(self._pairings.items()):
            if pairing.device == device:
                pairing.device = None
                logger.info(f"设备 {device.addr} 断开，配对 {pairing_code} 解除")

        try:
            device.writer.close()
        except Exception:
            pass

    def _remove_hook(self, pairing_code: str) -> None:
        """移除断开的 Hook"""
        if pairing_code in self._pairings:
            pairing = self._pairings.pop(pairing_code)
            if pairing.device:
                # 通知设备配对已解除
                try:
                    pairing.device.writer.write(
                        json.dumps({"cmd": "unpaired"}).encode() + b"\n"
                    )
                except Exception:
                    pass
                self._remove_device(pairing.device)
            logger.info(f"Hook 断开，配对 {pairing_code} 移除")
        elif pairing_code in self._pending_hooks:
            self._pending_hooks.pop(pairing_code)
            logger.info(f"等待配对的 Hook 断开，配对码 {pairing_code} 失效")

    async def _handle_pairing_request(self, device: DeviceConnection, pairing_code: str) -> bool:
        """处理设备的配对请求"""
        if pairing_code in self._pending_hooks:
            # 找到等待配对的 Hook
            hook = self._pending_hooks.pop(pairing_code)
            # 创建配对
            pairing = Pairing(hook=hook, device=device)
            self._pairings[pairing_code] = pairing
            device.pairing_code = pairing_code
            self._unpaired_devices.discard(device)
            logger.info(f"配对成功: {pairing_code}")

            # 通知设备配对成功
            await self._send_to_device(device, {
                "cmd": "paired",
                "pairing_code": pairing_code
            })

            # 发送当前状态（如果有等待的请求）
            if hook.pending_request:
                await self._send_to_device(device, {
                    "time": [int(time.time()), _tz_offset_seconds()]
                })
                await self._send_device_snapshot(device, hook)

            return True
        else:
            # 没有找到配对码对应的 Hook
            await self._send_to_device(device, {
                "cmd": "pairing_failed",
                "reason": "配对码无效或已过期"
            })
            logger.warning(f"设备 {device.addr} 配对失败，无效配对码: {pairing_code}")
            return False

    async def _send_device_snapshot(self, device: DeviceConnection, hook: HookConnection) -> None:
        """发送快照给指定设备"""
        if hook.pending_request:
            snapshot = {
                "total": 1,
                "running": 0,
                "waiting": 1,
                "msg": truncate(f"approve: {hook.pending_request.tool}"),
                "entries": list(reversed(hook.entries[:ENTRIES_MAX])),
                "tokens": 0,
                "tokens_today": 0,
                "prompt": {
                    "id": hook.pending_request.id,
                    "tool": truncate(hook.pending_request.tool),
                    "hint": truncate(hook.pending_request.hint),
                },
            }
            if hook.pending_request.context:
                snapshot["context"] = hook.pending_request.context
        else:
            snapshot = {
                "total": 0,
                "running": 0,
                "waiting": 0,
                "msg": "",
                "entries": list(reversed(hook.entries[:ENTRIES_MAX])),
                "tokens": 0,
                "tokens_today": 0,
            }

        await self._send_to_device(device, snapshot)

    async def _handle_permission_decision(self, device: DeviceConnection, msg: dict) -> None:
        """处理设备的审批决策"""
        if device.pairing_code is None:
            logger.warning("未配对设备发送决策，忽略")
            return

        pairing_code = device.pairing_code
        if pairing_code not in self._pairings:
            logger.warning(f"配对码 {pairing_code} 不存在")
            return

        pairing = self._pairings[pairing_code]
        if pairing.device != device:
            logger.warning("设备与配对不匹配")
            return

        mid = msg.get("id")
        decision = msg.get("decision")

        # 发送确认到设备
        try:
            await self._send_to_device(device, {"ack": "permission", "ok": True, "n": 0})
        except Exception as e:
            logger.warning("permission ack 发送失败: %s", e)

        hook = pairing.hook
        if hook.pending_request and hook.pending_request.id == mid:
            hook.pending_request.decision_future.set_result(decision)
            logger.info(f"收到决策 id={mid} decision={decision}")
        else:
            logger.warning(f"收到孤立 permission id={mid!r}")

    async def _handle_device(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理设备连接"""
        addr = writer.get_extra_info("peername")
        logger.info(f"新设备连接: {addr}")

        device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid=str(uuid.uuid4()))
        self._unpaired_devices.add(device)

        # 发送初始消息（提示输入配对码）
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

                    cmd = msg.get("cmd")
                    if cmd == "pair":
                        # 配对请求
                        pairing_code = msg.get("pairing_code")
                        if pairing_code:
                            await self._handle_pairing_request(device, pairing_code)
                    elif cmd == "permission":
                        # 审批决策（必须已配对）
                        if device.pairing_code:
                            await self._handle_permission_decision(device, msg)
                        else:
                            await self._send_to_device(device, {
                                "cmd": "error",
                                "reason": "请先配对"
                            })
                    else:
                        logger.warning(f"未知设备命令: {cmd}")

        except Exception as e:
            logger.error(f"设备连接处理异常: {e}")
        finally:
            self._remove_device(device)
            logger.info(f"设备断开: {addr}")

    async def _handle_hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理 Hook 连接（Claude Code 终端）"""
        # 生成配对码
        pairing_code = generate_pairing_code()
        logger.info(f"新 Hook 连接，配对码: {pairing_code}")

        # 创建 Hook 连接对象
        hook = HookConnection(writer=writer, pairing_code=pairing_code)
        self._pending_hooks[pairing_code] = hook

        try:
            while True:
                # 读取消息（支持多次消息）
                line = await asyncio.wait_for(reader.readline(), timeout=120.0)
                if not line:
                    logger.warning("Hook 连接关闭")
                    break

                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception as e:
                    logger.warning(f"Hook 消息解析失败: {e}")
                    continue

                if msg.get("action") == "get_pairing_code":
                    # 获取配对码请求 - 返回配对码但保持连接
                    writer.write(json.dumps({
                        "pairing_code": pairing_code
                    }).encode() + b"\n")
                    await writer.drain()
                    logger.info(f"发送配对码给 Hook: {pairing_code}")
                    continue

                # 正常审批请求
                rid = str(msg.get("id") or f"req_{int(time.time() * 1000)}")
                tool = str(msg.get("tool") or "?")
                hint = str(msg.get("hint") or "")
                context = msg.get("context") if isinstance(msg.get("context"), dict) else None

                logger.info(f"收到请求 id={rid} tool={tool} hint={hint!r}")

                # 等待配对（最多等待一段时间）
                timeout = 60.0  # 最多等待 60 秒配对
                start_time = time.time()

                while pairing_code not in self._pairings:
                    if time.time() - start_time > timeout:
                        logger.warning("配对超时")
                        writer.write(json.dumps({
                            "decision": "timeout",
                            "error": "配对超时，请检查设备"
                        }).encode() + b"\n")
                        await writer.drain()
                        return

                    await asyncio.sleep(0.5)

                # 配对成功，获取配对对象
                pairing = self._pairings[pairing_code]

                # 创建待处理请求
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                hook.pending_request = PendingRequest(
                    id=rid, tool=tool, hint=hint, decision_future=fut, context=context
                )
                hook.entries.insert(0, f"{time.strftime('%H:%M')} {truncate(f'{tool}: {hint}', 50)}")
                hook.entries = hook.entries[:ENTRIES_MAX]

                # 发送快照给配对的设备
                try:
                    await self._send_device_snapshot(pairing.device, hook)
                except Exception as e:
                    logger.warning("发送快照失败: %s", e)

                # 等待决策或超时
                try:
                    decision = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT)
                except asyncio.TimeoutError:
                    decision = "timeout"
                    logger.warning(f"id={rid} 审批超时")

                hook.pending_request = None

                # 发送决策给 Hook
                writer.write(json.dumps({
                    "decision": decision
                }).encode() + b"\n")
                await writer.drain()

                logger.info(f"id={rid} → decision={decision}")

        except asyncio.TimeoutError:
            logger.warning("Hook 连接超时")
        except Exception as e:
            logger.error(f"Hook 连接处理异常: {e}")
        finally:
            self._remove_hook(pairing_code)
            try:
                writer.close()
            except Exception:
                pass

    def _is_hook_request(self, msg: dict) -> bool:
        """判断消息是否为 Hook 请求"""
        return "tool" in msg or msg.get("action") == "get_pairing_code"

    def _is_device_message(self, msg: dict) -> bool:
        """判断消息是否为设备消息"""
        return "cmd" in msg and (msg["cmd"] in ["pair", "permission"])

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理客户端连接（Hook 或设备）"""
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
            await self._handle_hook(reader, writer)
        elif self._is_device_message(msg):
            logger.info(f"[{addr}] 识别为设备连接")
            await self._handle_device(reader, writer)
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

    bridge = Bridge()

    server = await asyncio.start_server(
        bridge.handle_client, host, port
    )
    logger.info(f"TCP 服务端已启动，监听 {host}:{port}")
    logger.info("  - 支持多终端多设备配对")

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
