"""claude-code-buddy-bridge 单元测试"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbb.bridge import (
    Bridge,
    Session,
    derive_pairing_code,
    DeviceConnection,
    PendingRequest,
)


def test_derive_pairing_code():
    code = derive_pairing_code("abc12345-def6-7890")
    assert code == "ABC12345"
    assert len(code) == 8


def test_derive_pairing_code_deterministic():
    sid = "a1b2c3d4-e5f6-7890"
    assert derive_pairing_code(sid) == derive_pairing_code(sid)


def test_derive_pairing_code_different_sessions():
    code1 = derive_pairing_code("aaa11111-0000-0000")
    code2 = derive_pairing_code("bbb22222-0000-0000")
    assert code1 != code2


def test_session_creation():
    session = Session(session_id="test-session-123", pairing_code="456789")
    assert session.session_id == "test-session-123"
    assert session.pairing_code == "456789"
    assert session.paired_devices == set()
    assert session.pending_requests == {}


def test_device_connection_defaults():
    reader = MagicMock()
    writer = MagicMock()
    addr = ("127.0.0.1", 12345)
    device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid="test-uid-123")
    assert device.session_id is None
    assert device.addr == addr
    assert device.uid == "test-uid-123"
    assert hash(device) == hash("test-uid-123")


def test_device_connection_equality():
    device1 = DeviceConnection(
        reader=MagicMock(), writer=MagicMock(),
        addr=("127.0.0.1", 12345), uid="same-uid",
    )
    device2 = DeviceConnection(
        reader=MagicMock(), writer=MagicMock(),
        addr=("127.0.0.1", 54321), uid="same-uid",
    )
    assert device1 == device2


def test_bridge_initial_state():
    bridge = Bridge()
    assert bridge._sessions == {}
    assert bridge._pairing_index == {}
    assert bridge._pending_pairings == {}
    assert bridge._unpaired_devices == set()


def test_is_hook_request():
    bridge = Bridge()
    assert bridge._is_hook_request({"hook_event_name": "PermissionRequest"})
    assert bridge._is_hook_request({"tool_name": "Bash"})
    assert bridge._is_hook_request({"action": "session_start"})
    assert bridge._is_hook_request({"action": "session_end"})
    assert not bridge._is_hook_request({"session_id": "abc123"})
    assert not bridge._is_hook_request({"cmd": "permission"})
    assert not bridge._is_hook_request({"cmd": "hello"})


def test_is_device_message():
    bridge = Bridge()
    assert bridge._is_device_message({"type": "hello"})
    assert bridge._is_device_message({"type": "pair", "data": {"pairing_code": "123456"}})
    assert bridge._is_device_message({"type": "decision", "data": {"behavior": "allow"}})
    assert not bridge._is_device_message({"tool_name": "Bash"})
    assert not bridge._is_device_message({"session_id": "abc"})


class TestBridgeSession:
    @pytest.fixture
    def bridge(self):
        return Bridge()

    @pytest.fixture
    def mock_writer(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.write = MagicMock()
        return writer

    def test_register_session(self, bridge, mock_writer):
        async def run_test():
            await bridge._register_session("a1b2c3d4-session-abc", mock_writer)

        asyncio.run(run_test())

        assert "a1b2c3d4-session-abc" in bridge._sessions
        session = bridge._sessions["a1b2c3d4-session-abc"]
        assert len(session.pairing_code) == 8
        assert session.pairing_code == "A1B2C3D4"
        assert bridge._pairing_index[session.pairing_code] == "a1b2c3d4-session-abc"

    def test_register_session_duplicate(self, bridge, mock_writer):
        async def run_test():
            await bridge._register_session("a1b2c3d4-session-abc", mock_writer)
            await bridge._register_session("a1b2c3d4-session-abc", mock_writer)

        asyncio.run(run_test())

        assert len(bridge._sessions) == 1
        code = bridge._sessions["a1b2c3d4-session-abc"].pairing_code
        assert bridge._pairing_index[code] == "a1b2c3d4-session-abc"

    def test_unregister_session(self, bridge, mock_writer):
        async def setup():
            await bridge._register_session("x9y8z7w6-session-xyz", mock_writer)
        asyncio.run(setup())

        session = bridge._sessions["x9y8z7w6-session-xyz"]
        code = session.pairing_code

        bridge._unregister_session("x9y8z7w6-session-xyz")

        assert "x9y8z7w6-session-xyz" not in bridge._sessions
        assert code not in bridge._pairing_index

    def test_unregister_session_notifies_devices(self, bridge, mock_writer):
        async def run_test():
            await bridge._register_session("aabbccdd-session-dev", mock_writer)
            session = bridge._sessions["aabbccdd-session-dev"]

            dev1 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="dev-1",
            )
            dev2 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12346), uid="dev-2",
            )
            session.paired_devices.add(dev1)
            session.paired_devices.add(dev2)
            dev1.session_id = "aabbccdd-session-dev"
            dev2.session_id = "aabbccdd-session-dev"

            bridge._unregister_session("aabbccdd-session-dev")

            assert dev1.session_id is None
            assert dev2.session_id is None
            assert dev1 in bridge._unpaired_devices
            assert dev2 in bridge._unpaired_devices

        asyncio.run(run_test())

    def test_pairing_code_uniqueness(self, bridge, mock_writer):
        async def run_test():
            for i in range(10):
                await bridge._register_session(f"{i:08x}-session", mock_writer)

        asyncio.run(run_test())

        codes = [s.pairing_code for s in bridge._sessions.values()]
        assert len(codes) == len(set(codes))

    def test_handle_pairing_success(self, bridge, mock_writer):
        async def run_test():
            await bridge._register_session("session-pair", mock_writer)
            session = bridge._sessions["session-pair"]
            code = session.pairing_code

            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="device-1",
            )
            bridge._unpaired_devices.add(device)

            result = await bridge._handle_pairing_request(device, code)

            assert result is True
            assert device in session.paired_devices
            assert device.session_id == "session-pair"
            assert device not in bridge._unpaired_devices

        asyncio.run(run_test())

    def test_handle_multi_device_pairing(self, bridge, mock_writer):
        async def run_test():
            await bridge._register_session("session-multi", mock_writer)
            session = bridge._sessions["session-multi"]
            code = session.pairing_code

            dev1 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="dev-m1",
            )
            dev2 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12346), uid="dev-m2",
            )
            bridge._unpaired_devices.add(dev1)
            bridge._unpaired_devices.add(dev2)

            r1 = await bridge._handle_pairing_request(dev1, code)
            r2 = await bridge._handle_pairing_request(dev2, code)

            assert r1 is True
            assert r2 is True
            assert len(session.paired_devices) == 2
            assert dev1 in session.paired_devices
            assert dev2 in session.paired_devices

        asyncio.run(run_test())

    def test_handle_pairing_unknown_code_stores_pending(self, bridge, mock_writer):
        """无效配对码 → 存为预配对（等待 session 注册）"""
        async def run_test():
            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="device-2",
            )
            bridge._unpaired_devices.add(device)

            result = await bridge._handle_pairing_request(device, "00000000")

            assert result is True
            assert "00000000" in bridge._pending_pairings
            assert device in bridge._pending_pairings["00000000"]

        asyncio.run(run_test())

    def test_pre_pairing_device_before_session(self, bridge, mock_writer):
        """设备先配对，CC 会话后启动时自动匹配"""
        async def run_test():
            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="device-pre",
            )
            bridge._unpaired_devices.add(device)

            # 设备先发送配对码（session 尚未注册）
            result = await bridge._handle_pairing_request(device, "AABB1122")
            assert result is True
            assert "AABB1122" in bridge._pending_pairings
            assert device in bridge._pending_pairings["AABB1122"]

            # CC 会话启动 → 自动配对
            await bridge._register_session("aabb1122-session-pre", mock_writer)
            session = bridge._sessions.get("aabb1122-session-pre")
            assert session is not None
            assert device in session.paired_devices
            assert device.session_id == "aabb1122-session-pre"
            assert "AABB1122" not in bridge._pending_pairings

        asyncio.run(run_test())

    def test_pre_pairing_multiple_devices(self, bridge, mock_writer):
        """多个设备预配对，CC 启动时全部自动匹配"""
        async def run_test():
            dev1 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="dev-pre1",
            )
            dev2 = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12346), uid="dev-pre2",
            )
            bridge._unpaired_devices.add(dev1)
            bridge._unpaired_devices.add(dev2)

            await bridge._handle_pairing_request(dev1, "CCDD3344")
            await bridge._handle_pairing_request(dev2, "CCDD3344")

            await bridge._register_session("ccdd3344-session-multi", mock_writer)
            session = bridge._sessions["ccdd3344-session-multi"]
            assert len(session.paired_devices) == 2
            assert dev1 in session.paired_devices
            assert dev2 in session.paired_devices

        asyncio.run(run_test())

    def test_pre_pairing_auto_recovery(self, bridge, mock_writer):
        """PermissionRequest 触发自动恢复时，预配对设备也应被匹配"""
        async def run_test():
            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="device-recovery",
            )
            bridge._unpaired_devices.add(device)

            # 设备先配对
            await bridge._handle_pairing_request(device, "EEFF5566")
            assert "EEFF5566" in bridge._pending_pairings

            # PermissionRequest 触发自动恢复（不走 session_start）
            event = {"session_id": "eeff5566-session-recovery", "tool_name": "Bash", "tool_input": {"command": "ls"}}
            # 需要一个 reader mock 来处理 _watch_hook
            mock_reader = MagicMock()
            mock_reader.read = AsyncMock(return_value=b"")
            await bridge._process_permission_request(event, mock_writer, mock_reader)

            session = bridge._sessions.get("eeff5566-session-recovery")
            assert session is not None
            assert device in session.paired_devices
            assert device.session_id == "eeff5566-session-recovery"
            assert "EEFF5566" not in bridge._pending_pairings

        asyncio.run(run_test())


class TestQrCode:
    def test_qr_to_terminal_returns_string(self):
        from ccbb.qrcode import qr_to_terminal
        result = qr_to_terminal("https://ccbb.dev/pair?code=456789")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_qr_to_terminal_contains_blocks(self):
        from ccbb.qrcode import qr_to_terminal
        result = qr_to_terminal("https://ccbb.dev/pair?code=456789")
        # 应该包含 Unicode 半块字符
        assert any(c in result for c in "▀▄█")

    def test_qr_to_terminal_has_multiple_lines(self):
        from ccbb.qrcode import qr_to_terminal
        result = qr_to_terminal("https://ccbb.dev/pair?code=456789")
        lines = result.strip().splitlines()
        assert len(lines) >= 5

    def test_qr_to_terminal_short_text(self):
        from ccbb.qrcode import qr_to_terminal
        result = qr_to_terminal("hello")
        assert isinstance(result, str)
        assert len(result) > 0


class TestSuggestions:
    @pytest.fixture
    def bridge(self):
        return Bridge()

    @pytest.fixture
    def mock_writer(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.write = MagicMock()
        return writer

    def test_pending_request_suggestions(self):
        import asyncio
        from ccbb.bridge import PendingRequest
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        req = PendingRequest(
            id="test-id", decision_future=fut,
            raw={"tool_name": "Bash", "tool_input": {"command": "ls"},
                 "permission_suggestions": [{"rules": [{"behavior": "allow"}]}]},
        )
        assert req.raw.get("permission_suggestions") is not None
        loop.close()

    def test_request_forwarded_to_device(self, bridge, mock_writer):
        """验证审批请求通过统一格式广播给设备"""
        async def run_test():
            await bridge._register_session("session-fwd", mock_writer)
            session = bridge._sessions["session-fwd"]

            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="dev-fwd",
            )
            session.paired_devices.add(device)
            device.session_id = "session-fwd"

            event = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                     "permission_suggestions": [{"rules": [{"behavior": "allow", "toolName": "Bash"}]}]}

            await bridge._broadcast(session, "request", event)

            import json
            calls = mock_writer.write.call_args_list
            last_payload = calls[-1][0][0]
            msg = json.loads(last_payload.decode())
            assert msg["type"] == "request"
            assert msg["data"]["tool_name"] == "Bash"
            assert msg["data"]["permission_suggestions"] is not None

        asyncio.run(run_test())

    def test_permission_decision_transparent(self, bridge, mock_writer):
        """验证设备决策直接透传（无 cmd/id 包装）"""
        async def run_test():
            await bridge._register_session("session-up", mock_writer)
            session = bridge._sessions["session-up"]

            device = DeviceConnection(
                reader=MagicMock(), writer=mock_writer,
                addr=("127.0.0.1", 12345), uid="dev-up",
            )
            session.paired_devices.add(device)
            device.session_id = "session-up"

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            session.pending_requests["req-up"] = PendingRequest(
                id="req-up", decision_future=fut,
                raw={"tool_name": "Bash", "tool_input": {"command": "ls"}},
            )

            # 设备直接发送 CC decision 格式（含请求 ID）
            updated_perms = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "ls"}], "behavior": "allow", "destination": "localSettings"}]
            await bridge._handle_permission_decision(device, {
                "behavior": "allow",
                "ccbb_request_id": "req-up",
                "updatedPermissions": updated_perms,
            })

            result = fut.result()
            assert isinstance(result, dict)
            assert result["behavior"] == "allow"
            assert result["updatedPermissions"] == updated_perms
            # decision 就是设备发送的原始数据，bridge 不添加也不剥离

        asyncio.run(run_test())
