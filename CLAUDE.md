# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

`claude-code-buddy-bridge`（CLI: `ccbb`）是一个纯 TCP 桥接服务，让 ESP32 等设备充当 Claude Code CLI 的物理审批按钮。纯 Python 3.11+，零运行时依赖（仅标准库）。电脑作为 TCP 服务端（默认 `0.0.0.0:9876`），设备主动连接。支持多会话多设备配对。

## 开发命令

```bash
uv sync --extra dev                                   # 安装全部依赖（含 pytest、ruff）
uv run ruff check src/                                # 代码检查
uv run pytest -v                                      # 运行全部测试
uv run pytest -v tests/test_bridge.py::test_name      # 运行单个测试
uv run ccbb daemon                                    # 启动守护进程
uv run ccbb daemon -v                                 # 调试模式
uv run ccbb install                                   # 注入 hook 到 ~/.claude/settings.json
uv run ccbb install --tools Bash Write                # 只拦截指定工具
uv run ccbb uninstall                                 # 移除 hook
```

环境变量：`CCBB_TCP_HOST`（默认 `0.0.0.0`）、`CCBB_TCP_PORT`（默认 `9876`）。

## 架构

所有通信使用 TCP + JSON lines（换行分隔），消息格式统一为 `{"type": "...", "data": {...}}`：

```
Claude Code → hook.py (stdin JSON) → TCP 127.0.0.1:9876 → bridge.py (守护进程) → TCP 0.0.0.0:9876 → 设备
```

所有连接共用同一个 TCP 端口，通过首条消息自动识别连接类型（Hook 或设备）。

### 核心模块

- **`src/ccbb/hook.py`** — Claude Code hook 处理三种事件：`SessionStart`（注册会话、显示配对码）、`PermissionRequest`（透传审批请求）、`SessionEnd`（通知清理）。Fail-open 设计。支持 `updatedPermissions` 回传。
- **`src/ccbb/bridge.py`** — 守护进程核心。`Session` 管理每个会话的配对码、配对设备和挂起请求队列。`_pairing_index` 提供 O(1) 配对码→会话查找。
- **`src/ccbb/cli.py`** — argparse CLI，注册三种 hook（SessionStart、PermissionRequest、SessionEnd）。
- **`examples/tcp_device_client.py`** — 交互式测试客户端，支持显示工具详情和"记住规则"选项。

### 配对机制

每个 Claude Code 会话独立配对：

1. `SessionStart` hook 触发 → Bridge 注册会话并生成 6 位随机配对码 → hook 通过 stderr 显示醒目配对码
2. 设备发送配对码 → Bridge 将设备绑定到该会话
3. 审批请求只发送给配对的设备，设备决策只返回给配对的 Hook
4. `SessionEnd` hook 触发 → Bridge 清理配对、通知设备会话已关闭

### 连接识别

通过首条消息自动区分连接类型：
- `type` 字段为 `hello`/`pair`/`decision` → 设备连接（优先判断）
- 含 `hook_event_name`、`action`（`session_start`/`session_end`）或 `tool_name` 字段 → Hook 连接

### 消息协议

**设备 → Bridge：**

| type | data | 说明 |
|------|------|------|
| `hello` | `{}` | 连接握手 |
| `pair` | `{pairing_code}` | 配对请求 |
| `decision` | `{behavior, ccbb_request_id, ...}` | 审批决策 |

**Bridge → 设备：**

| type | data | 说明 |
|------|------|------|
| `waiting_pairing` | `{message}` | 等待配对 |
| `paired` | `{pairing_code, session_id}` | 配对成功 |
| `pairing_failed` | `{reason}` | 配对失败 |
| `request` | 原始 CC 事件 | 审批请求 |
| `done` | `{id, decision}` | 审批完成 |
| `session_end` | `{session_id}` | 会话结束 |

**Hook → Bridge：** `{"action":"session_start","session_id":"..."}`、`{"session_id":"...","tool_name":"...","tool_input":{...},"tool_use_id":"..."}`、`{"action":"session_end","session_id":"..."}`

**Bridge → Hook 响应：**
- SessionStart: `{"pairing_code":"123456"}`
- PermissionRequest: `{"behavior":"allow","ccbb_request_id":"..."}` 或超时返回 `{"behavior":"closed"}`
- SessionEnd: 无响应（fire-and-forget）

## 提交规范

格式：`type: 中文描述`（如 `feat: 将仓库通信改为TCP`、`fix: 修复 lint 和测试问题`）。
