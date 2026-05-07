"""claude-code-buddy-bridge 单元测试"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbb.bridge import (
    Bridge,
    truncate,
    generate_pairing_code_from_session,
    SessionInfo,
    DeviceConnection,
)


def test_truncate_ascii_passthrough():
    assert truncate("rm -rf /tmp/foo") == "rm -rf /tmp/foo"


def test_truncate_preserves_unicode():
    result = truncate("删除文件 /tmp/foo")
    assert "删除" in result
    assert "/tmp/foo" in result


def test_truncate_truncates():
    long_text = "a" * 200
    assert len(truncate(long_text, max_len=60)) == 60


def test_generate_pairing_code_consistent():
    session_id = "test-session-12345678"
    code1 = generate_pairing_code_from_session(session_id)
    code2 = generate_pairing_code_from_session(session_id)
    assert code1 == code2
    assert len(code1) == 6
    assert code1.isdigit()


def test_generate_pairing_code_different_sessions():
    code1 = generate_pairing_code_from_session("session-1")
    code2 = generate_pairing_code_from_session("session-2")
    assert code1 != code2


def test_session_info_creation():
    session = SessionInfo(session_id="test-session", pairing_code="123456")
    assert session.session_id == "test-session"
    assert session.pairing_code == "123456"
    assert session.pending_request is None
    assert session.entries == []


def test_device_connection_hashable():
    reader = MagicMock()
    writer = MagicMock()
    addr = ("127.0.0.1", 12345)
    device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid="test-uid-123")
    assert device.pairing_code is None
    assert device.addr == addr
    assert device.uid == "test-uid-123"
    assert hash(device) == hash("test-uid-123")


def test_device_connection_equality():
    device1 = DeviceConnection(
        reader=MagicMock(), writer=MagicMock(), 
        addr=("127.0.0.1", 12345), uid="same-uid"
    )
    device2 = DeviceConnection(
        reader=MagicMock(), writer=MagicMock(), 
        addr=("127.0.0.1", 54321), uid="same-uid"
    )
    assert device1 == device2


def test_bridge_initial_state():
    bridge = Bridge()
    assert bridge._sessions == {}
    assert bridge._pairings == {}
    assert bridge._unpaired_devices == set()
    assert bridge._pending_decisions == {}


def test_is_hook_request():
    bridge = Bridge()
    assert bridge._is_hook_request({"action": "session_start"})
    assert bridge._is_hook_request({"session_id": "abc123", "tool": "Bash"})
    assert not bridge._is_hook_request({"cmd": "permission"})


def test_is_device_message():
    bridge = Bridge()
    assert bridge._is_device_message({"cmd": "pair"})
    assert bridge._is_device_message({"cmd": "permission"})
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

    def test_handle_session_start(self, bridge, mock_writer):
        async def run_test():
            msg = {"action": "session_start", "session_id": "test-session-id-123"}
            await bridge._handle_session_start(msg, mock_writer)
        
        asyncio.run(run_test())

        assert "test-session-id-123" in bridge._sessions
        session = bridge._sessions["test-session-id-123"]
        assert session.pairing_code.isdigit()
        assert len(session.pairing_code) == 6

    def test_handle_pairing_request(self, bridge, mock_writer):
        session_id = "test-session-for-pairing"
        pairing_code = generate_pairing_code_from_session(session_id)
        bridge._sessions[session_id] = SessionInfo(
            session_id=session_id, pairing_code=pairing_code
        )

        device = DeviceConnection(
            reader=MagicMock(), writer=mock_writer, 
            addr=("127.0.0.1", 12345), uid="device-1"
        )
        bridge._unpaired_devices.add(device)

        async def run_test():
            result = await bridge._handle_pairing_request(device, pairing_code)
            return result

        result = asyncio.run(run_test())

        assert result is True
        assert pairing_code in bridge._pairings
        assert device.pairing_code == pairing_code


class TestHookFunctions:
    def test_make_hint_command(self):
        from ccbb.hook import _make_hint
        assert _make_hint({"command": "ls -la", "other": "ignored"}) == "ls -la"

    def test_make_hint_file_path(self):
        from ccbb.hook import _make_hint
        assert _make_hint({"file_path": "/etc/hosts"}) == "/etc/hosts"

    def test_make_hint_fallback_json(self):
        from ccbb.hook import _make_hint
        result = _make_hint({"unknown_key": "value"})
        assert "unknown_key" in result

    def test_make_hint_non_dict(self):
        from ccbb.hook import _make_hint
        assert _make_hint("raw string") == "raw string"

    def test_make_hint_truncated(self):
        from ccbb.hook import _make_hint, HINT_MAX
        long_val = "x" * 300
        assert len(_make_hint({"command": long_val})) == HINT_MAX
