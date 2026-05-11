#!/usr/bin/env python3
"""端到端测试：模拟多 hook 并发 + 设备审批"""
import asyncio
import json
import sys
import time

HOST = "127.0.0.1"
PORT = 9876


async def tcp_exchange(payload: dict, read_timeout: float = 5.0) -> dict | None:
    """发送一条 JSON，读取一条响应"""
    reader, writer = await asyncio.open_connection(HOST, PORT)
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()

    buf = bytearray()
    try:
        while b"\n" not in buf:
            data = await asyncio.wait_for(reader.read(4096), timeout=read_timeout)
            if not data:
                break
            buf.extend(data)
    except asyncio.TimeoutError:
        pass

    writer.close()
    await writer.wait_closed()

    if not buf:
        return None
    line = bytes(buf).split(b"\n", 1)[0].strip()
    return json.loads(line.decode()) if line else None


async def hook_request(session_id: str, tool: str, cmd: str, req_id: str, timeout: float = 10.0):
    """模拟一个 hook 发送 PermissionRequest 并等待响应"""
    reader, writer = await asyncio.open_connection(HOST, PORT)
    event = {
        "session_id": session_id,
        "tool_name": tool,
        "tool_input": {"command": cmd},
        "tool_use_id": req_id,
    }
    writer.write((json.dumps(event) + "\n").encode())
    await writer.drain()

    # 等待响应
    buf = bytearray()
    try:
        while b"\n" not in buf:
            data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not data:
                break
            buf.extend(data)
    except asyncio.TimeoutError:
        pass

    writer.close()
    await writer.wait_closed()

    if buf:
        line = bytes(buf).split(b"\n", 1)[0].strip()
        return json.loads(line.decode()) if line else None
    return None


async def device_connect(session_id: str | None = None):
    """创建一个设备连接，返回 (reader, writer, 状态收集器)"""
    reader, writer = await asyncio.open_connection(HOST, PORT)

    # 读取欢迎消息
    welcome = await _read_json(reader)
    print(f"  [设备] 收到: {welcome}")

    received = []  # 收到的所有消息

    async def listen():
        while True:
            msg = await _read_json(reader)
            if msg is None:
                break
            received.append(msg)
            print(f"  [设备] 收到: {_brief(msg)}")

    return reader, writer, received, listen


async def _read_json(reader: asyncio.StreamReader) -> dict | None:
    buf = bytearray()
    while b"\n" not in buf:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        except asyncio.TimeoutError:
            return None
        if not data:
            return None
        buf.extend(data)
    line = bytes(buf).split(b"\n", 1)[0].strip()
    return json.loads(line.decode()) if line else None


def _brief(msg: dict) -> str:
    t = msg.get("type", "")
    d = msg.get("data", {})
    if t == "waiting_pairing":
        return "type=waiting_pairing"
    if t == "request":
        return f"type=request tool={d.get('tool_name')} id={d.get('tool_use_id','?')}"
    if t == "done":
        return f"type=done decision={d.get('decision')} id={d.get('id','?')}"
    return json.dumps(msg, ensure_ascii=False)[:100]


async def main():
    print("=" * 60)
    print("端到端测试：多 hook 并发 + 设备审批")
    print("=" * 60)

    session_id = f"test-{int(time.time())}"

    # ── Step 1: 注册 session ────────────────────────────
    print(f"\n[1] 注册 session: {session_id[:16]}...")
    resp = await tcp_exchange({"action": "session_start", "session_id": session_id})
    assert resp and "pairing_code" in resp, f"注册失败: {resp}"
    code = resp["pairing_code"]
    print(f"  配对码: {code}")

    # ── Step 2: 设备连接并配对 ──────────────────────────
    print(f"\n[2] 设备连接并配对")
    dev_reader, dev_writer, dev_received, dev_listen = await device_connect()

    # 配对
    dev_writer.write((json.dumps({"cmd": "pair", "pairing_code": code}) + "\n").encode())
    await dev_writer.drain()

    listen_task = asyncio.create_task(dev_listen())

    await asyncio.sleep(0.5)

    # ── Step 3: 发送 3 个并发的 PermissionRequest ───────
    print(f"\n[3] 发送 3 个并发 PermissionRequest")
    tasks = [
        asyncio.create_task(hook_request(session_id, "Bash", "ls /tmp", "req-001")),
        asyncio.create_task(hook_request(session_id, "Bash", "ls /var", "req-002")),
        asyncio.create_task(hook_request(session_id, "Bash", "ls /opt", "req-003")),
    ]

    # 等待设备收到所有请求
    await asyncio.sleep(1.0)
    requests_on_device = [m for m in dev_received if m.get("type") == "request"]
    print(f"\n  设备收到的请求数: {len(requests_on_device)}")
    for r in requests_on_device:
        d = r.get("data", {})
        print(f"    - {d.get('tool_name')} {d.get('tool_input', {}).get('command')} (id={d.get('tool_use_id')})")

    # ── Step 4: 逐个审批（模拟设备发送决策）──────────
    print(f"\n[4] 设备逐个审批（bridge 控制推送节奏）")

    for i in range(3):
        # 等待设备收到请求
        await asyncio.sleep(0.3)
        req_msgs = [m for m in dev_received if m.get("type") == "request"]
        if i < len(req_msgs):
            req_msg = req_msgs[i]
        else:
            # 可能在 done 消息后才收到下一个请求
            await asyncio.sleep(0.5)
            req_msgs = [m for m in dev_received if m.get("type") == "request"]
            req_msg = req_msgs[i] if i < len(req_msgs) else None

        if not req_msg:
            print(f"  等待请求 {i+1} 超时")
            continue

        rid = req_msg.get("data", {}).get("tool_use_id")
        decision = {"type": "decision", "data": {"behavior": "allow", "ccbb_request_id": rid}}
        print(f"  审批 {rid}: {decision}")
        dev_writer.write((json.dumps(decision) + "\n").encode())
        await dev_writer.drain()
        await asyncio.sleep(0.5)

    # ── Step 5: 等待所有 hook 收到响应 ──────────────────
    print(f"\n[5] 等待 hook 响应")
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"  hook-{i+1}: {r}")

    # ── Step 6: 验证 ───────────────────────────────────
    print(f"\n[6] 验证结果")
    done_msgs = [m for m in dev_received if m.get("type") == "done"]
    print(f"  设备收到的 done 消息数: {len(done_msgs)}")
    for d in done_msgs:
        dd = d.get("data", {})
        print(f"    - done decision={dd.get('decision')} id={dd.get('id')}")

    ok = True
    if len(requests_on_device) != 3:
        print(f"  FAIL: 设备应收到 3 个请求，实际 {len(requests_on_device)}")
        ok = False
    if len(done_msgs) != 3:
        print(f"  FAIL: 设备应收到 3 个 done，实际 {len(done_msgs)}")
        ok = False
    if len([r for r in results if r and r.get("behavior") == "allow"]) != 3:
        print(f"  FAIL: 3 个 hook 都应收到 allow")
        ok = False

    if ok:
        print("\n  ALL PASSED ✓")
    else:
        print("\n  FAILED ✗")

    # 清理
    listen_task.cancel()
    try:
        await listen_task
    except asyncio.CancelledError:
        pass
    dev_writer.close()
    await dev_writer.wait_closed()

    # 结束 session
    await tcp_exchange({"action": "session_end", "session_id": session_id}, read_timeout=1.0)
    print(f"\n  Session 已清理")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
