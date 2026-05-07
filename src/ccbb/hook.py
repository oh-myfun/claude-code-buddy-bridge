"""
ccbb.hook — Claude Code Hook 处理

支持两种 hook 事件：
1. SessionStart - session 开始时生成配对码
2. PermissionRequest - 审批请求时发送到配对的设备

CC hook 协议（stdout JSON）
  允许: {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                 "decision": {"behavior": "allow"}}}
  拒绝: {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                 "decision": {"behavior": "deny", "message": "..."}}}
  透传: exit(0) 且 stdout 无内容 → CC 显示自己的权限对话框

Fail-open 设计
  守护进程未运行、连接超时、任何异常 → exit(0) 无输出 → CC 自己处理
"""

from __future__ import annotations

import json
import socket
import sys

HOOK_HOST = "127.0.0.1"
HOOK_PORT = 9876

CONNECT_TIMEOUT = 1.0
READ_TIMEOUT = 115.0
HINT_MAX = 200

_HINT_KEYS = ("command", "file_path", "url", "path", "pattern", "query", "prompt", "input")


def _make_hint(tool_input: object) -> str:
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


def _fail_open() -> None:
    sys.exit(0)


def _emit_allow() -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def _emit_deny(message: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": message},
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def _connect_to_bridge() -> socket.socket | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.connect((HOOK_HOST, HOOK_PORT))
        return s
    except (socket.timeout, OSError, ConnectionRefusedError):
        return None


def _send_request(s: socket.socket, payload: bytes) -> dict | None:
    try:
        s.sendall(payload)
        s.settimeout(READ_TIMEOUT)

        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)

        line = bytes(buf).split(b"\n", 1)[0].strip()
        if not line:
            return None

        return json.loads(line.decode("utf-8"))
    except (socket.timeout, OSError):
        return None


def _handle_session_start(event: dict) -> None:
    session_id = event.get("session_id", "")
    if not session_id:
        _fail_open()
        return

    s = _connect_to_bridge()
    if not s:
        _fail_open()
        return

    try:
        payload = json.dumps({
            "action": "session_start",
            "session_id": session_id,
        }) + "\n"
        resp = _send_request(s, payload.encode("utf-8"))

        if resp and "pairing_code" in resp:
            pairing_code = resp["pairing_code"]
            print(f"\n{'=' * 50}", file=sys.stderr)
            print("  Claude Code Buddy Bridge", file=sys.stderr)
            print(f"  设备配对码: {pairing_code}", file=sys.stderr)
            print("  请在审批设备上输入此配对码", file=sys.stderr)
            print(f"{'=' * 50}\n", file=sys.stderr)
            sys.stderr.flush()
    finally:
        try:
            s.close()
        except OSError:
            pass

    _fail_open()


def _handle_permission_request(event: dict) -> None:
    session_id = event.get("session_id", "")
    if not session_id:
        _fail_open()
        return

    tool_use_id = event.get("tool_use_id") or f"hook_{id(event)}"
    tool_name = event.get("tool_name") or "?"
    tool_input = event.get("tool_input")

    req = {
        "session_id": session_id,
        "id": str(tool_use_id),
        "tool": str(tool_name),
        "hint": _make_hint(tool_input),
        "context": event,
    }

    s = _connect_to_bridge()
    if not s:
        _fail_open()
        return

    try:
        payload = json.dumps(req, separators=(",", ":"), ensure_ascii=False) + "\n"
        resp = _send_request(s, payload.encode("utf-8"))

        if resp and "decision" in resp:
            decision = resp["decision"]
            if decision == "once":
                _emit_allow()
            elif decision == "deny":
                _emit_deny("已通过 ccbb 拒绝此操作")
    finally:
        try:
            s.close()
        except OSError:
            pass

    _fail_open()


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        _fail_open()
        return

    hook_event = event.get("hook_event_name", "")

    if hook_event == "SessionStart":
        _handle_session_start(event)
    elif hook_event == "PermissionRequest":
        _handle_permission_request(event)
    else:
        _fail_open()


if __name__ == "__main__":
    main()
