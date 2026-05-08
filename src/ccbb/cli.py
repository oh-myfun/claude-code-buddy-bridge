"""
ccbb.cli — 命令行入口

用法:
  ccbb daemon                  启动守护进程（作为 TCP 服务端，监听设备连接）
  ccbb install                 自动注入 Claude Code hook 配置
  ccbb uninstall               移除 Claude Code hook 配置
  ccbb status                  检查守护进程是否在线
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from pathlib import Path

from ccbb import __version__


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_daemon(args: argparse.Namespace) -> None:
    _setup_logging(args.verbose)
    from ccbb.bridge import run
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def cmd_status(_args: argparse.Namespace) -> None:
    from ccbb.bridge import TCP_PORT_DEFAULT
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(("127.0.0.1", TCP_PORT_DEFAULT))
        s.close()
        print("● ccbb 守护进程：运行中 ✓")
    except (ConnectionRefusedError, socket.timeout, OSError):
        print("● ccbb 守护进程：未运行")
        sys.exit(1)


def cmd_install(args: argparse.Namespace) -> None:
    _setup_logging()

    hook_script = Path(sys.executable).parent / "ccbb-hook"
    hook_py = Path(__file__).parent / "hook.py"

    if hook_script.exists():
        command = str(hook_script).replace("\\", "/")
    else:
        command = f'"{sys.executable}" "{hook_py}"'.replace("\\", "/")

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            print(f"警告：无法解析 {settings_path}，将创建新文件")

    hooks_block = existing.setdefault("hooks", {})

    hook_entry = {
        "type": "command",
        "command": command,
        "timeout": 120,
    }

    session_start_hooks = hooks_block.setdefault("SessionStart", [])
    permission_hooks = hooks_block.setdefault("PermissionRequest", [])
    session_end_hooks = hooks_block.setdefault("SessionEnd", [])

    def is_ccbb_entry(entry: dict) -> bool:
        return any(
            h.get("command", "").find("claude-code-buddy-bridge") >= 0 or
            h.get("command", "").find("ccbb") >= 0
            for h in entry.get("hooks", [])
        )

    session_start_hooks[:] = [e for e in session_start_hooks if not is_ccbb_entry(e)]
    permission_hooks[:] = [e for e in permission_hooks if not is_ccbb_entry(e)]
    session_end_hooks[:] = [e for e in session_end_hooks if not is_ccbb_entry(e)]

    session_start_entry = {"hooks": [hook_entry]}
    session_start_hooks.append(session_start_entry)

    session_end_entry = {"hooks": [hook_entry]}
    session_end_hooks.append(session_end_entry)

    matchers = args.tools if args.tools else [""]
    for matcher in matchers:
        entry: dict = {"hooks": [hook_entry]}
        if matcher:
            entry["matcher"] = matcher
        permission_hooks.append(entry)

    settings_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"✓ ccbb hook 已写入 {settings_path}")
    print(f"  命令: {command}")
    print("  SessionStart hook: 已添加（用于显示配对码）")
    print("  SessionEnd hook: 已添加（用于清理配对）")
    print(f"  PermissionRequest hook: 覆盖范围: {'所有工具' if not args.tools else ', '.join(args.tools)}")
    print()
    print("下一步：")
    print("  1. 运行 'ccbb daemon' 启动守护进程")
    print("  2. 开启 Claude Code，终端会显示配对码")
    print("  3. 在审批设备上输入配对码完成配对")


def cmd_uninstall(_args: argparse.Namespace) -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print("未找到 Claude Code 配置文件，无需操作。")
        return

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读取配置失败: {e}")
        sys.exit(1)

    hooks_block = data.get("hooks", {})

    def is_ccbb_entry(entry: dict) -> bool:
        return any(
            h.get("command", "").find("claude-code-buddy-bridge") >= 0 or
            h.get("command", "").find("ccbb") >= 0
            for h in entry.get("hooks", [])
        )

    removed = 0
    for hook_type in ["SessionStart", "PermissionRequest", "SessionEnd"]:
        hooks_list = hooks_block.get(hook_type, [])
        before = len(hooks_list)
        hooks_list[:] = [e for e in hooks_list if not is_ccbb_entry(e)]
        removed += before - len(hooks_list)

    if removed == 0:
        print("未找到 ccbb hook 条目，无需操作。")
        return

    settings_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"✓ 已移除 {removed} 条 ccbb hook（{settings_path}）")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-code-buddy-bridge",
        description="claude-code-buddy-bridge — Claude Code CLI ←─ TCP ── 设备作为客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  ccbb install                 # 注入 hook（覆盖所有工具）
  ccbb install --tools Bash    # 只拦截 Bash 工具
  ccbb daemon                  # 启动守护进程（TCP 服务端监听 0.0.0.0:9876）
  ccbb daemon -v              # 调试模式（显示详细日志）
  ccbb status                  # 检查守护进程是否在线
  ccbb uninstall               # 移除 hook
  CCBB_TCP_HOST=192.168.1.100 CCBB_TCP_PORT=8888 ccbb daemon  # 自定义监听地址
""",
    )
    parser.add_argument("-V", "--version", action="version", version=f"claude-code-buddy-bridge {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="启动 TCP 守护进程（作为服务端）")
    p_daemon.add_argument("-v", "--verbose", action="store_true", help="显示详细日志")
    p_daemon.set_defaults(func=cmd_daemon)

    p_status = sub.add_parser("status", help="检查守护进程是否在线")
    p_status.set_defaults(func=cmd_status)

    p_install = sub.add_parser("install", help="自动注入 Claude Code hook 配置")
    p_install.add_argument(
        "--tools", nargs="+", metavar="TOOL",
        help="限定拦截的工具名（默认拦截所有工具）。例: --tools Bash Write",
    )
    p_install.add_argument("--force", action="store_true", help="强制覆盖已有配置")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="移除 Claude Code hook 配置")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
