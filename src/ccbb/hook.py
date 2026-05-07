"""
ccbb.hook — Claude Code PermissionRequest hook

Claude Code 在每次需要用户授权工具调用时执行此脚本。
脚本通过 Unix Socket（Unix）或 TCP（Windows）连接守护进程，把决策结果翻译为 CC hook 协议。

CC hook 协议（stdout JSON）
  允许: {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                 "decision": {"behavior": "allow"}}}
  拒绝: {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                 "decision": {"behavior": "deny", "message": "..."}}}
  透传: exit(0) 且 stdout 无内容 → CC 显示自己的权限对话框

Fail-open 设计
  守护进程未运行、连接超时、任何异常 → exit(0) 无输出 → CC 自己处理
  这保证了 ccbb 不在线时不会阻断任何操作。
"""

from __future__ import annotations

import json
import platform
import socket
import sys

IS_WINDOWS = platform.system() == "Windows"

# TCP 连接配置（与 bridge 共用同一端口）
HOOK_HOST = "127.0.0.1"
HOOK_PORT = 9876  # hook 连接端口（仅本地访问）

CONNECT_TIMEOUT = 1.0  # 连接超时（秒）
READ_TIMEOUT = 115.0   # 等待决策超时，必须小于 CC hook timeout（120s）
HINT_MAX = 200

# 按优先级依次尝试提取操作摘要的字段
_HINT_KEYS = ("command", "file_path", "url", "path", "pattern", "query", "prompt", "input")


def _make_hint(tool_input: object) -> str:
    """从 tool_input 中提取最有意义的摘要字符串。"""
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


# ── CC 协议输出 ────────────────────────────────────────────────────────────────

def _fail_open() -> None:
    """不输出任何内容，退出 0 → CC 走自己的权限对话框。"""
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


# ── 与守护进程通信 ─────────────────────────────────────────────────────────────

def _connect_to_bridge() -> socket.socket | None:
    """
    建立到守护进程的 TCP 连接。
    任何异常都返回 None → fail-open。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.connect((HOOK_HOST, HOOK_PORT))
        return s
    except (socket.timeout, OSError, ConnectionRefusedError):
        return None


def _send_request(s: socket.socket, payload: bytes) -> dict | None:
    """
    通过已建立的连接发送请求并接收响应。
    """
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


def _get_pairing_code(s: socket.socket) -> str | None:
    """
    获取配对码。
    """
    payload = json.dumps({"action": "get_pairing_code"}) + "\n"
    resp = _send_request(s, payload.encode("utf-8"))
    if resp and "pairing_code" in resp:
        return str(resp["pairing_code"])
    return None


def _send_permission_request(s: socket.socket, req: dict) -> str | None:
    """
    发送审批请求并等待决策。
    """
    payload = json.dumps(req, separators=(",", ":"), ensure_ascii=False) + "\n"
    resp = _send_request(s, payload.encode("utf-8"))
    if resp and "decision" in resp:
        return str(resp["decision"])
    return None


# ── 主逻辑 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        _fail_open()
        return

    tool_use_id = event.get("tool_use_id") or f"hook_{id(event)}"
    tool_name = event.get("tool_name") or "?"
    tool_input = event.get("tool_input")

    req = {
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
        # 先获取配对码
        pairing_code = _get_pairing_code(s)
        if not pairing_code:
            _fail_open()
            return

        # 输出配对码到 stderr（Claude Code 会显示这个）
        print(f"设备配对码: {pairing_code}", file=sys.stderr)
        sys.stderr.flush()

        # 发送审批请求
        decision = _send_permission_request(s, req)

        if decision == "once":
            _emit_allow()
        elif decision == "deny":
            _emit_deny("已通过 ccbb 拒绝此操作")
        else:
            _fail_open()
    finally:
        try:
            s.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
