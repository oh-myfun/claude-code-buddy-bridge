"""claude-code-buddy-bridge 单元测试"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from ccbb.bridge import (
    Bridge,
    truncate,
    generate_pairing_code,
    HookConnection,
    DeviceConnection,
    Pairing,
    PendingRequest,
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


def test_generate_pairing_code():
    code = generate_pairing_code()
    assert len(code) == 6
    assert code.isdigit()
    assert 100000 <= int(code) <= 999999


def test_generate_pairing_code_unique():
    codes = [generate_pairing_code() for _ in range(100)]
    assert len(set(codes)) > 90


def test_pending_request_creation():
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    req = PendingRequest(
        id="req_001",
        tool="Bash",
        hint="rm -rf /tmp",
        decision_future=fut,
        context={"foo": "bar"}
    )
    assert req.id == "req_001"
    assert req.tool == "Bash"
    assert req.hint == "rm -rf /tmp"
    assert req.context == {"foo": "bar"}
    loop.close()


def test_hook_connection_default():
    writer = MagicMock()
    hook = HookConnection(writer=writer, pairing_code="123456")
    assert hook.pairing_code == "123456"
    assert hook.pending_request is None
    assert hook.entries == []


def test_device_connection_default():
    reader = MagicMock()
    writer = MagicMock()
    addr = ("127.0.0.1", 12345)
    device = DeviceConnection(reader=reader, writer=writer, addr=addr, uid="test-uid-123")
    assert device.pairing_code is None
    assert device.addr == addr
    assert device.uid == "test-uid-123"
    assert hash(device) == hash("test-uid-123")


def test_pairing_creation():
    writer = MagicMock()
    hook = HookConnection(writer=writer, pairing_code="123456")
    pairing = Pairing(hook=hook)
    assert pairing.hook == hook
    assert pairing.device is None


def test_bridge_initial_state():
    bridge = Bridge()
    assert bridge._pairings == {}
    assert bridge._unpaired_devices == set()
    assert bridge._pending_hooks == {}


def test_is_hook_request():
    bridge = Bridge()
    assert bridge._is_hook_request({"tool": "Bash"})
    assert bridge._is_hook_request({"action": "get_pairing_code"})
    assert not bridge._is_hook_request({"cmd": "permission"})
    assert not bridge._is_hook_request({"cmd": "pair"})


def test_is_device_message():
    bridge = Bridge()
    assert bridge._is_device_message({"cmd": "pair"})
    assert bridge._is_device_message({"cmd": "permission"})
    assert not bridge._is_device_message({"tool": "Bash"})
    assert not bridge._is_device_message({"action": "get_pairing_code"})


class TestBridgePairing:
    @pytest.fixture
    def bridge(self):
        return Bridge()

    @pytest.fixture
    def mock_hook_writer(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        return writer

    @pytest.fixture
    def mock_device_writer(self):
        writer = MagicMock()
        writer.drain = AsyncMock()
        return writer

    def test_handle_pairing_success(self, bridge, mock_hook_writer, mock_device_writer):
        reader = MagicMock()
        addr = ("127.0.0.1", 12345)
        device = DeviceConnection(reader=reader, writer=mock_device_writer, addr=addr, uid="test-device-1")

        hook = HookConnection(writer=mock_hook_writer, pairing_code="654321")
        bridge._pending_hooks["654321"] = hook

        async def run_test():
            return await bridge._handle_pairing_request(device, "654321")

        result = asyncio.run(run_test())

        assert result is True
        assert "654321" in bridge._pairings
        assert bridge._pairings["654321"].device == device
        assert device.pairing_code == "654321"

    def test_handle_pairing_failure(self, bridge, mock_device_writer):
        reader = MagicMock()
        addr = ("127.0.0.1", 12345)
        device = DeviceConnection(reader=reader, writer=mock_device_writer, addr=addr, uid="test-device-2")

        async def run_test():
            return await bridge._handle_pairing_request(device, "000000")

        result = asyncio.run(run_test())

        assert result is False
        assert "000000" not in bridge._pairings
        mock_device_writer.write.assert_called()


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
