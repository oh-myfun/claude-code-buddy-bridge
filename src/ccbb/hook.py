"""
ccbb.hook — Claude Code Hook 处理

支持三种 hook 事件：
1. SessionStart — 会话启动时注册到 bridge（配对码在 daemon 终端显示）
2. PermissionRequest — 审批请求时发送到配对的设备
3. SessionEnd — 会话结束时通知 bridge 清理配对

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
import os
import socket
import sys
import time


HOOK_HOST = os.environ.get("CCBB_TCP_HOST", "127.0.0.1")
HOOK_PORT = int(os.environ.get("CCBB_TCP_PORT", "9876"))

CONNECT_TIMEOUT = 1.0  # 连接超时（秒）
POLL_INTERVAL = 2.0    # 每次 recv 超时（秒），短间隔轮询以便快速感知 CC 终止
MAX_WAIT = 118.0       # 总等待上限，必须小于 CC hook timeout（120s）


# ── CC 协议输出 ────────────────────────────────────────────────────────────────

def _fail_open() -> None:
    """不输出任何内容，退出 0 → CC 走自己的权限对话框。"""
    sys.exit(0)


# ── 与守护进程通信 ─────────────────────────────────────────────────────────────

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

        buf = bytearray()
        deadline = time.monotonic() + MAX_WAIT

        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            s.settimeout(min(POLL_INTERVAL, remaining))

            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf.extend(chunk)

        line = bytes(buf).split(b"\n", 1)[0].strip()
        if not line:
            return None

        return json.loads(line.decode("utf-8"))
    except OSError:
        return None


# ── SessionStart ───────────────────────────────────────────────────────────

def _handle_session_start(event: dict) -> None:
    """注册会话到 bridge（配对码在 daemon 终端显示）"""
    session_id = event.get("session_id", "")
    if not session_id:
        _fail_open()
        return

    s = _connect_to_bridge()
    if not s:
        _fail_open()
        return

    try:
        payload_data: dict = {
            "action": "session_start",
            "session_id": session_id,
        }
        cwd = event.get("cwd", "")
        if cwd:
            payload_data["cwd"] = cwd
        payload = (json.dumps(payload_data) + "\n").encode("utf-8")
        _send_request(s, payload)
    except Exception:
        pass
    finally:
        try:
            s.close()
        except OSError:
            pass

    _fail_open()


# ── SessionEnd ─────────────────────────────────────────────────────────────

def _handle_session_end(event: dict) -> None:
    """通知 bridge 清理会话（fire-and-forget）"""
    session_id = event.get("session_id", "")
    if not session_id:
        sys.exit(0)

    s = _connect_to_bridge()
    if not s:
        sys.exit(0)

    try:
        s.settimeout(1.0)
        payload = (json.dumps({
            "action": "session_end",
            "session_id": session_id,
        }) + "\n").encode("utf-8")
        s.sendall(payload)
    except Exception:
        pass
    finally:
        try:
            s.close()
        except OSError:
            pass

    sys.exit(0)


# ── Status (UserPromptSubmit / Stop / StopFailure) ─────────────────────────

def _fire_status(event: dict, state: str) -> None:
    """向 bridge 发送 CC 状态变化（fire-and-forget）"""
    session_id = event.get("session_id", "")
    if not session_id:
        sys.exit(0)

    s = _connect_to_bridge()
    if not s:
        sys.exit(0)

    try:
        s.settimeout(1.0)
        status_data: dict = {"state": state}
        if state == "running":
            prompt = event.get("prompt", "")
            if prompt:
                status_data["prompt"] = prompt[:200]
        elif state == "idle":
            msg = event.get("last_assistant_message", "")
            if msg:
                status_data["message"] = msg[:200]
        elif state == "error":
            msg = event.get("error", "") or event.get("error_details", "")
            if msg:
                status_data["message"] = msg[:200]

        payload = (json.dumps({
            "action": "status",
            "session_id": session_id,
            "status": status_data,
        }) + "\n").encode("utf-8")
        s.sendall(payload)
    except Exception:
        pass
    finally:
        try:
            s.close()
        except OSError:
            pass

    sys.exit(0)


def _handle_user_prompt_submit(event: dict) -> None:
    _fire_status(event, "running")


def _handle_stop(event: dict) -> None:
    _fire_status(event, "idle")


def _handle_stop_failure(event: dict) -> None:
    _fire_status(event, "error")


# ── PermissionRequest ──────────────────────────────────────────────────────

def _handle_permission_request(event: dict) -> None:
    """发送审批请求到配对设备（透传原始事件）"""
    session_id = event.get("session_id", "")
    if not session_id:
        _fail_open()
        return

    s = _connect_to_bridge()
    if not s:
        _fail_open()
        return

    try:
        payload = (json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        resp = _send_request(s, payload)

        if resp and "behavior" in resp:
            # bridge 透传的 decision 对象，直接包装输出
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": resp,
                }
            }
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
            sys.exit(0)
    finally:
        try:
            s.close()
        except OSError:
            pass

    _fail_open()


# ── 主入口 ─────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        _fail_open()
        return

    hook_event = event.get("hook_event_name", "")

    if hook_event == "SessionStart":
        _handle_session_start(event)
    elif hook_event == "SessionEnd":
        _handle_session_end(event)
    elif hook_event == "PermissionRequest":
        _handle_permission_request(event)
    elif hook_event == "UserPromptSubmit":
        _handle_user_prompt_submit(event)
    elif hook_event == "Stop":
        _handle_stop(event)
    elif hook_event == "StopFailure":
        _handle_stop_failure(event)
    else:
        _fail_open()


if __name__ == "__main__":
    main()
