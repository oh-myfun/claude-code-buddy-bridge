#!/usr/bin/env python3
"""
示例 TCP 设备客户端
模拟一个简单的审批按钮设备，作为 TCP 客户端连接到服务端

协议说明：
- 所有来自 bridge 的消息使用统一格式 {"type": "...", "data": {...}}
- 设备发送 CC decision 格式（{"behavior": "allow", ...}）或配对命令
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
    pending_event: Optional[dict] = None  # 原始 CC 事件
    paired: bool = False
    pairing_code: Optional[str] = None
    session_id: Optional[str] = None


# 从 tool_input 中提取关键信息的字段（按优先级）
_HINT_KEYS = ("command", "file_path", "url", "path", "pattern", "query", "prompt", "input", "description")


def _extract_tool_info(event: dict) -> str:
    """从原始 CC 事件中提取 tool_input 的关键信息"""
    tool_input = event.get("tool_input")
    tool_name = event.get("tool_name", "?")

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
    # AskUserQuestion: questions + options
    elif tool_name == "AskUserQuestion":
        idx = 0
        for q in tool_input.get("questions", []):
            header = q.get("header", "")
            question = q.get("question", "")
            multi = q.get("multiSelect", False)
            mode = "多选" if multi else "单选"
            parts.append(f"  {'[' + header + '] ' if header else ''}{question} ({mode})")
            for opt in q.get("options", []):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                line = f"    [{idx}] {label}"
                if desc:
                    line += f" — {desc[:100]}"
                parts.append(line)
                idx += 1
    else:
        for key in _HINT_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val:
                parts.append(f"  {key}: {val[:200]}")

    return "\n".join(parts) if parts else json.dumps(tool_input, indent=2, ensure_ascii=False)[:200]


def _format_suggestions(suggestions: list) -> str:
    """格式化审批规则建议"""
    lines = []
    idx = 0
    for sug in suggestions:
        sug_type = sug.get("type", "addRules")
        behavior = sug.get("behavior", "allow")
        dest = sug.get("destination", "")
        rules = sug.get("rules") or []

        if sug_type == "setMode":
            lines.append(f"  [{idx}] 切换模式: {sug.get('mode', '?')} → {dest}")
            idx += 1
        else:
            for rule in rules:
                tool_name = rule.get("toolName") or "*"
                content = rule.get("ruleContent") or ""
                bl = "允许" if behavior == "allow" else "拒绝" if behavior == "deny" else behavior
                desc = f"{bl} {tool_name}" + (f": {content}" if content else "")
                lines.append(f"  [{idx}] {desc} → {dest}")
                idx += 1

    return "\n".join(lines)


def _print_request(event: dict) -> None:
    """打印审批请求信息"""
    tool_name = event.get("tool_name", "?")
    suggestions = event.get("permission_suggestions")
    is_question = tool_name == "AskUserQuestion"

    print(f"\n{'=' * 60}")
    print(f"  审批请求 | {tool_name}")

    tool_info = _extract_tool_info(event)
    if tool_info:
        print(tool_info)

    if is_question:
        print(f"{'=' * 60}")
        print("  输入编号回答（多选用逗号或空格分隔），[D]拒绝  [C]查看原始数据")
    else:
        if suggestions:
            print(f"\n  可记住规则:")
            print(_format_suggestions(suggestions))
        print(f"{'=' * 60}")
        opts = "[A]允许  [D]拒绝  [C]查看原始数据"
        if suggestions:
            opts += "\n  [R]记住全部规则  [编号]记住指定规则（如 0 或 0,2）"
        print(f"  {opts}")


async def user_input_task(writer: asyncio.StreamWriter, state: DeviceState):
    """处理用户输入"""
    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break

            cmd = line.strip()

            if cmd.lower() == 'q':
                print("[设备] 退出…")
                break
            elif not state.paired:
                if len(cmd) == 6 and cmd.isdigit():
                    resp = {"type": "pair", "data": {"pairing_code": cmd}}
                    print(f"[设备] 发送配对请求: {resp}")
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                else:
                    print("[设备] 请输入6位配对码")
            elif cmd.lower() == 'c' and state.pending_event:
                print(f"\n{'=' * 60}")
                print(json.dumps(state.pending_event, indent=2, ensure_ascii=False))
                print(f"{'=' * 60}\n")
            elif cmd.lower() == 'a' and state.pending_event:
                decision = {"behavior": "allow"}
                tid = state.pending_event.get("tool_use_id")
                if tid:
                    decision["ccbb_request_id"] = tid
                msg = {"type": "decision", "data": decision}
                print(f"[设备] 发送允许: {msg}")
                writer.write((json.dumps(msg) + "\n").encode())
                await writer.drain()
                state.pending_event = None
            elif cmd.lower() == 'r' and state.pending_event:
                # 记住全部规则并允许
                suggestions = state.pending_event.get("permission_suggestions")
                if suggestions:
                    decision = {"behavior": "allow", "updatedPermissions": suggestions}
                    tid = state.pending_event.get("tool_use_id")
                    if tid:
                        decision["ccbb_request_id"] = tid
                    msg = {"type": "decision", "data": decision}
                    print(f"[设备] 发送允许并记住全部规则: {msg}")
                    writer.write((json.dumps(msg) + "\n").encode())
                    await writer.drain()
                    state.pending_event = None
                else:
                    print("[设备] 没有可记住的规则")
            elif re.match(r'^[\d,，\s]+$', cmd.strip()) and state.pending_event:
                tool_name = state.pending_event.get("tool_name", "?")
                try:
                    indices = [int(x) for x in re.split(r'[,，\s]+', cmd.strip()) if x]

                    if tool_name == "AskUserQuestion":
                        # 构建扁平选项索引
                        questions = state.pending_event.get("tool_input", {}).get("questions", [])
                        flat = []
                        for qi, q in enumerate(questions):
                            for oi, opt in enumerate(q.get("options", [])):
                                flat.append((qi, oi, opt["label"], q))

                        selected_labels = []
                        answers = {}
                        for idx in indices:
                            if idx >= len(flat):
                                print(f"[设备] 无效选项: {idx}")
                                break
                            qi, oi, label, q = flat[idx]
                            question_text = q.get("question", "")
                            multi = q.get("multiSelect", False)
                            if multi:
                                answers.setdefault(question_text, []).append(label)
                            else:
                                answers[question_text] = label
                            selected_labels.append(label)

                        decision = {
                            "behavior": "allow",
                            "updatedInput": {"questions": questions, "answers": answers},
                        }
                        tid = state.pending_event.get("tool_use_id")
                        if tid:
                            decision["ccbb_request_id"] = tid
                        msg = {"type": "decision", "data": decision}
                        print(f"[设备] 选择: {', '.join(selected_labels)}")
                        print(f"[设备] 发送回答: {msg}")
                        writer.write((json.dumps(msg) + "\n").encode())
                        await writer.drain()
                        state.pending_event = None
                    else:
                        # 选择记住规则（支持多个，逗号分隔）
                        suggestions = state.pending_event.get("permission_suggestions") or []
                        flat_rules = []
                        for sug in suggestions:
                            if sug.get("type") == "setMode":
                                flat_rules.append(("setMode", sug, None))
                            else:
                                for rule in sug.get("rules") or []:
                                    flat_rules.append(("rule", sug, rule))

                        selected_perms = []
                        for idx in indices:
                            if idx >= len(flat_rules):
                                print(f"[设备] 无效编号: {idx}")
                                continue
                            kind, sug, rule = flat_rules[idx]
                            if kind == "setMode":
                                selected_perms.append(sug)
                            else:
                                selected_perms.append({
                                    "type": sug.get("type", "addRules"),
                                    "rules": [rule],
                                    "behavior": sug.get("behavior"),
                                    "destination": sug.get("destination"),
                                })

                        if not selected_perms:
                            continue
                        decision = {
                            "behavior": "allow",
                            "updatedPermissions": selected_perms,
                        }
                        tid = state.pending_event.get("tool_use_id")
                        if tid:
                            decision["ccbb_request_id"] = tid
                        msg = {"type": "decision", "data": decision}
                        print(f"[设备] 发送允许并记住规则: {msg}")
                        writer.write((json.dumps(msg) + "\n").encode())
                        await writer.drain()
                        state.pending_event = None
                except (ValueError, IndexError):
                    print("[设备] 无效的编号")
            elif cmd.lower() == 'd' and state.pending_event:
                decision = {
                    "behavior": "deny",
                    "message": "已通过 ccbb 拒绝此操作",
                }
                tid = state.pending_event.get("tool_use_id")
                if tid:
                    decision["ccbb_request_id"] = tid
                msg = {"type": "decision", "data": decision}
                print(f"[设备] 发送拒绝: {msg}")
                writer.write((json.dumps(msg) + "\n").encode())
                await writer.drain()
                state.pending_event = None
            elif state.pending_event:
                print("请选择: [A]允许  [D]拒绝  [C]查看原始数据  或输入编号")
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
    writer.write((json.dumps({"type": "hello", "data": {}}) + "\n").encode())
    await writer.drain()

    state = DeviceState()

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

                # 统一消息格式 {"type": "...", "data": {...}}
                msg_type = msg.get("type")
                data = msg.get("data", {})
                if msg_type == "paired":
                    state.paired = True
                    state.pairing_code = data.get("pairing_code")
                    state.session_id = data.get("session_id")
                    sid = state.session_id[:8] if state.session_id else "?"
                    print(f"[设备] 配对成功! session: {sid}...")
                    print("[设备] 等待审批请求...")
                elif msg_type == "pairing_failed":
                    print(f"[设备] 配对失败: {data.get('reason', '未知原因')}")
                    print("[设备] 请重新输入配对码")
                elif msg_type == "waiting_pairing":
                    print(f"[设备] {data.get('message', '等待配对')}")
                elif msg_type == "session_end":
                    state.paired = False
                    state.pairing_code = None
                    state.session_id = None
                    sid = data.get("session_id", "?")
                    print(f"[设备] 会话已结束 (session: {sid[:8] if len(sid) > 8 else sid}...)")
                    print("[设备] 请输入新的配对码")
                elif msg_type == "done":
                    # 审批完成通知
                    done_id = data.get("id", "?")
                    if state.pending_event and state.pending_event.get("tool_use_id") == done_id:
                        state.pending_event = None
                    print(f"\n[设备] 审批已完成 (id={done_id}, decision={data.get('decision')})")
                    print("[设备] 等待下一个审批请求...")
                elif msg_type == "request":
                    # 审批请求
                    state.pending_event = data
                    _print_request(data)
                else:
                    print(f"[设备] 收到: {msg}")

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
