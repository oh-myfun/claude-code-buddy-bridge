#!/usr/bin/env python3
"""
示例 TCP 设备客户端
模拟一个简单的审批按钮设备，作为 TCP 客户端连接到服务端
"""

import asyncio
import json
import sys
import os
import re
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
    session_id: Optional[str] = None
    suggestions: Optional[list] = None


# 从 tool_input 中提取关键信息的字段（按优先级）
_HINT_KEYS = ("command", "file_path", "url", "path", "pattern", "query", "prompt", "input", "description")


def _extract_tool_info(context: dict) -> str:
    """从 context 中提取 tool_input 的关键信息"""
    tool_input = context.get("tool_input")
    tool_name = context.get("tool_name", "?")

    if not isinstance(tool_input, dict):
        return str(tool_input) if tool_input else ""

    parts = []

    # Bash: command + description
    if tool_name == "Bash":
        if tool_input.get("command"):
            parts.append(f"  命令: {tool_input['command'][:200]}")
        if tool_input.get("description"):
            parts.append(f"  说明: {tool_input['description'][:200]}")
    # Write/Edit: file_path + content
    elif tool_name in ("Write", "Edit"):
        if tool_input.get("file_path"):
            parts.append(f"  文件: {tool_input['file_path']}")
        content = tool_input.get("content") or tool_input.get("new_text") or tool_input.get("old_string")
        if content:
            parts.append(f"  内容: {str(content)[:200]}")
    # Agent: description + prompt
    elif tool_name == "Agent":
        if tool_input.get("description"):
            parts.append(f"  描述: {tool_input['description'][:200]}")
        if tool_input.get("prompt"):
            parts.append(f"  提示: {str(tool_input['prompt'])[:200]}")
    else:
        for key in _HINT_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val:
                parts.append(f"  {key}: {val[:200]}")

    return "\n".join(parts) if parts else json.dumps(tool_input, indent=2, ensure_ascii=False)[:200]


def _format_suggestions(suggestions: list) -> str:
    """格式化审批规则建议"""
    lines = []
    for i, sug in enumerate(suggestions):
        sug_type = sug.get("type", "addRules")
        behavior = sug.get("behavior", "allow")
        dest = sug.get("destination", "")
        rules = sug.get("rules") or sug.get("addRules") or []

        if sug_type == "setMode":
            lines.append(f"  [{i}] 切换模式: {sug.get('mode', '?')} → {dest}")
        else:
            for j, rule in enumerate(rules):
                tool_name = rule.get("toolName") or rule.get("tool", "*")
                content = rule.get("ruleContent") or rule.get("content", "")
                bl = "允许" if behavior == "allow" else "拒绝" if behavior == "deny" else behavior
                desc = f"{bl} {tool_name}" + (f": {content}" if content else "")
                lines.append(f"  [{i}.{j}] {desc} → {dest}")

    return "\n".join(lines)


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
                print("[设备] 完整上下文:")
                print(f"{json.dumps(state.context, indent=2, ensure_ascii=False)}")
                print(f"{'=' * 60}\n")
                if state.pending:
                    opts = "[A]允许  "
                    if state.suggestions:
                        opts += "[R]记住规则  "
                    opts += "[D]拒绝  [C]详情  [Q]退出"
                    print(f"请选择: {opts}")
            elif cmd.lower() == 'a' and state.pending:
                # 发送允许
                resp = {
                    "cmd": "permission",
                    "id": state.pending.get("id"),
                    "behavior": "allow",
                }
                print(f"[设备] 发送允许: {resp}")
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                state.pending = None
                state.context = None
                state.suggestions = None
            elif cmd.lower() == 'r' and state.pending and state.suggestions:
                # 记住规则 — 显示可用规则让用户选择
                print(f"\n{'=' * 60}")
                print("[设备] 可记住的审批规则:")
                print(_format_suggestions(state.suggestions))
                print(f"{'=' * 60}")
                print("输入规则编号（如 0.0）或 [C] 取消")
            elif cmd.lower().startswith('r') and state.pending and state.suggestions:
                pass  # handled above
            elif re.match(r'\d+\.\d+', cmd) and state.pending and state.suggestions:
                # 选择记住规则
                try:
                    si, ri = cmd.split('.')
                    sug = state.suggestions[int(si)]
                    rules = sug.get("rules") or sug.get("addRules") or []
                    if sug.get("type") == "setMode":
                        selected_rule = sug
                    else:
                        rule = rules[int(ri)]
                        selected_rule = {
                            "type": sug.get("type", "addRules"),
                            "rules": [rule],
                            "behavior": sug.get("behavior"),
                            "destination": sug.get("destination"),
                        }
                    resp = {
                        "cmd": "permission",
                        "id": state.pending.get("id"),
                        "behavior": "allow",
                        "updatedPermissions": [selected_rule],
                    }
                    print(f"[设备] 发送允许并记住规则: {resp}")
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                    state.pending = None
                    state.context = None
                    state.suggestions = None
                except (ValueError, IndexError):
                    print("[设备] 无效的规则编号")
            elif cmd.lower() == 'd' and state.pending:
                # 发送拒绝
                resp = {
                    "cmd": "permission",
                    "id": state.pending.get("id"),
                    "behavior": "deny",
                    "message": "已通过 ccbb 拒绝此操作",
                }
                print(f"[设备] 发送拒绝: {resp}")
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
                state.pending = None
                state.context = None
                state.suggestions = None
            elif state.pending:
                print("请选择: [A]允许  [R]记住规则  [D]拒绝  [C]详情  [Q]退出")
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

    print("[设备] 已连接到服务端")

    # 发送 hello 消息用于连接识别
    writer.write((json.dumps({"cmd": "hello"}) + "\n").encode())
    await writer.drain()

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
                except Exception:
                    print(f"[设备] 收到无效 JSON: {line}")
                    continue

                cmd = msg.get("cmd")
                if cmd == "paired":
                    # 配对成功
                    state.paired = True
                    state.pairing_code = msg.get("pairing_code")
                    state.session_id = msg.get("session_id")
                    sid = state.session_id[:8] if state.session_id else "?"
                    print(f"[设备] 配对成功! session: {sid}...")
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
                    state.session_id = None
                    print("[设备] 配对已解除，请重新配对")
                elif cmd == "session_end":
                    # 会话结束
                    state.paired = False
                    state.pairing_code = None
                    sid = msg.get("session_id", "?")
                    print(f"[设备] 会话已结束 (session: {sid[:8]}...)")
                    print("[设备] 请输入新的配对码")
                elif cmd == "permission_done":
                    # 审批完成通知
                    done_id = msg.get("id", "?")
                    done_decision = msg.get("decision", "?")
                    state.pending = None
                    state.context = None
                    state.suggestions = None
                    print(f"\n[设备] 审批已完成 (id={done_id}, decision={done_decision})")
                    print("[设备] 等待下一个审批请求...")
                elif "time" in msg:
                    print(f"[设备] 收到时间同步: {msg['time']}")
                elif "ack" in msg:
                    print(f"[设备] 收到确认: {msg}")
                else:
                    # 这是快照消息
                    state.entries = msg.get("entries", [])
                    state.suggestions = msg.get("suggestions")
                    if msg.get("waiting", 0) > 0:
                        state.pending = msg.get("prompt", {})
                        state.context = msg.get("context")
                        tool = state.pending.get("tool", "?")
                        hint = state.pending.get("hint", "")
                        print(f"\n{'=' * 60}")
                        print(f"  审批请求 | {tool}")
                        if state.context:
                            tool_info = _extract_tool_info(state.context)
                            if tool_info:
                                print(tool_info)
                            else:
                                print(f"  {hint}")
                            print("  [C] 查看完整上下文")
                        else:
                            print(f"  {hint}")
                        if state.suggestions:
                            print(f"\n  可记住规则:")
                            print(_format_suggestions(state.suggestions))
                            print("  [R] 选择记住规则")
                        print(f"{'=' * 60}")
                        opts = "[A]允许  "
                        if state.suggestions:
                            opts += "[R]记住规则  "
                        opts += "[D]拒绝  [C]详情  [Q]退出"
                        print(f"  请选择: {opts}")
                    else:
                        state.pending = None
                        state.context = None
                        state.suggestions = None
                        print("[设备] 当前无待审批请求")
                        if state.entries:
                            print("[设备] 历史记录:")
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
        except Exception:
            pass
        print("[设备] 已断开连接")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[设备] 已停止")
