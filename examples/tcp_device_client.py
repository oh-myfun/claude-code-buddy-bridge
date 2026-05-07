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
    context: Optional[dict] = None
    entries: list[str] = field(default_factory=list)
    paired: bool = False
    pairing_code: Optional[str] = None


async def user_input_task(writer: asyncio.StreamWriter, state: DeviceState):
    """处理用户输入"""
    loop = asyncio.get_event_loop()

    while True:
        try:
            # 在单独线程中读取输入，避免阻塞事件循环
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            cmd = line.strip()

            if cmd.lower() == 'q':
                print("[设备] 退出…")
                break
            elif not state.paired:
                # 未配对状态：输入配对码
                if len(cmd) == 6 and cmd.isdigit():
                    # 发送配对请求
                    resp = {
                        "cmd": "pair",
                        "pairing_code": cmd
                    }
                    print(f"[设备] 发送配对请求: {resp}")
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                else:
                    print("[设备] 请输入6位配对码")
            elif cmd.lower() == 'c' and state.context:
                # 显示完整上下文
                print(f"\n{'=' * 60}")
                print(f"[设备] 完整上下文:")
                print(f"{json.dumps(state.context, indent=2, ensure_ascii=False)}")
                print(f"{'=' * 60}\n")
                if state.pending:
                    print("请选择: [A]允许  [D]拒绝  [C]查看上下文  [Q]退出")
            elif cmd.lower() == 'a' and state.pending:
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
                state.context = None
            elif cmd.lower() == 'd' and state.pending:
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
                state.context = None
            elif state.pending:
                print("请选择: [A]允许  [D]拒绝  [C]查看上下文  [Q]退出")
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

                cmd = msg.get("cmd")
                if cmd == "paired":
                    # 配对成功
                    state.paired = True
                    state.pairing_code = msg.get("pairing_code")
                    print(f"[设备] 配对成功! 配对码: {state.pairing_code}")
                    print("[设备] 等待审批请求...")
                elif cmd == "pairing_failed":
                    # 配对失败
                    print(f"[设备] 配对失败: {msg.get('reason', '未知原因')}")
                    print("[设备] 请重新输入配对码")
                elif cmd == "waiting_pairing":
                    # 等待配对
                    print(f"[设备] {msg.get('message', '等待配对')}")
                elif cmd == "unpaired":
                    # 配对解除
                    state.paired = False
                    state.pairing_code = None
                    print("[设备] 配对已解除，请重新配对")
                elif "time" in msg:
                    print(f"[设备] 收到时间同步: {msg['time']}")
                elif "ack" in msg:
                    print(f"[设备] 收到确认: {msg}")
                else:
                    # 这是快照消息
                    state.entries = msg.get("entries", [])
                    if msg.get("waiting", 0) > 0:
                        state.pending = msg.get("prompt", {})
                        state.context = msg.get("context")
                        print(f"\n{'=' * 60}")
                        print(f"[设备] 收到审批请求!")
                        print(f"  ID: {state.pending.get('id')}")
                        print(f"  工具: {state.pending.get('tool')}")
                        print(f"  提示: {state.pending.get('hint')}")
                        if state.context:
                            print(f"  提示: 按 [C] 查看完整上下文")
                        print(f"{'=' * 60}")
                        print(f"请选择: [A]允许  [D]拒绝  [C]查看上下文  [Q]退出")
                    else:
                        state.pending = None
                        state.context = None
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
