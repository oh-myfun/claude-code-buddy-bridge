#!/usr/bin/env python3
"""
示例 TCP 设备客户端
模拟一个简单的审批按钮设备，作为 TCP 客户端连接到服务端
"""

import asyncio
import json
import sys
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeviceState:
    """模拟设备状态"""
    pending: Optional[dict] = None
    entries: list[str] = field(default_factory=list)


async def user_input_task(writer: asyncio.StreamWriter, state: DeviceState):
    """处理用户输入"""
    loop = asyncio.get_event_loop()

    while True:
        try:
            # 在单独线程中读取输入，避免阻塞事件循环
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            cmd = line.strip().lower()

            if cmd == 'q':
                print("[设备] 退出…")
                break
            elif cmd == 'a' and state.pending:
                # 发送允许
                resp = {
                    "cmd": "permission",
                    "id": state.pending.get("id"),
                    "decision": "once"
                }
                print(f"[设备] 发送允许: {resp}")
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                state.pending = None
            elif cmd == 'd' and state.pending:
                # 发送拒绝
                resp = {
                    "cmd": "permission",
                    "id": state.pending.get("id"),
                    "decision": "deny"
                }
                print(f"[设备] 发送拒绝: {resp}")
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                state.pending = None
            elif state.pending:
                print("请选择: [A]允许  [D]拒绝  [Q]退出")
            else:
                print("当前无待审批请求，请输入命令")

        except Exception as e:
            print(f"[设备] 输入处理错误: {e}")


async def main():
    """主函数 - 作为 TCP 客户端连接到服务端"""
    host = os.environ.get("CCBB_TCP_HOST", "127.0.0.1")
    port = int(os.environ.get("CCBB_TCP_PORT", "9876"))

    print(f"[设备] 正在连接到 TCP 服务端 {host}:{port}…")

    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception as e:
        print(f"[设备] 连接失败: {e}")
        print("[设备] 请确保 ccbb daemon 正在运行")
        return

    print(f"[设备] 已连接到服务端")
    state = DeviceState()

    # 启动用户输入任务
    input_task = asyncio.create_task(user_input_task(writer, state))

    try:
        rx_buf = bytearray()
        while True:
            data = await reader.read(4096)
            if not data:
                print("[设备] 连接断开")
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
                    print(f"[设备] 收到无效 JSON: {line}")
                    continue

                if "time" in msg:
                    print(f"[设备] 收到时间同步: {msg['time']}")
                elif "ack" in msg:
                    print(f"[设备] 收到确认: {msg}")
                else:
                    # 这是快照消息
                    state.entries = msg.get("entries", [])
                    if msg.get("waiting", 0) > 0:
                        state.pending = msg.get("prompt", {})
                        print(f"\n{'=' * 50}")
                        print(f"[设备] 收到审批请求!")
                        print(f"  ID: {state.pending.get('id')}")
                        print(f"  工具: {state.pending.get('tool')}")
                        print(f"  提示: {state.pending.get('hint')}")
                        print(f"{'=' * 50}")
                        print(f"请选择: [A]允许  [D]拒绝  [Q]退出")
                    else:
                        state.pending = None
                        print(f"[设备] 当前无待审批请求")
                        if state.entries:
                            print(f"[设备] 历史记录:")
                            for entry in state.entries:
                                print(f"  - {entry}")

    except Exception as e:
        print(f"[设备] 错误: {e}")
    finally:
        input_task.cancel()
        try:
            await input_task
        except asyncio.CancelledError:
            pass

        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass
        print("[设备] 已断开连接")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[设备] 已停止")
