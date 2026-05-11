# Claude Code 审批协议说明

本文档描述 Claude Code 通过 hook 机制发送的审批请求格式，以及 ccbb 设备端应如何响应。
适用于 TCP 设备客户端和 Web 审批页面的开发者。

---

## 1. 请求格式（Claude Code → Hook → Bridge → 设备）

所有审批请求由 bridge 透传，设备端收到的就是 hook 从 Claude Code 拿到的原始数据（加上 `tool`/`hint` 摘要字段）。

```json
{
  "session_id": "sess-uuid",
  "id": "req-uuid",
  "tool": "Bash",
  "hint": "rm -rf node_modules",
  "context": {
    "hook_event_name": "PermissionRequest",
    "session_id": "sess-uuid",
    "tool_name": "Bash",
    "tool_input": { ... },
    "permission_suggestions": [ ... ]
  }
}
```

| 字段 | 说明 |
|------|------|
| `session_id` | 会话 ID |
| `id` | 请求 ID |
| `tool` | 工具名称摘要（由 hook 生成） |
| `hint` | 操作摘要（由 hook 从 tool_input 提取） |
| `context` | Claude Code 的完整事件数据 |
| `context.tool_name` | 真实工具名（`Bash`, `Write`, `AskUserQuestion` 等） |
| `context.tool_input` | 工具输入参数（按工具类型不同） |
| `context.permission_suggestions` | CC 建议的权限规则（可选） |

---

## 2. 请求类型

### 2.1 Bash 命令

```json
{
  "context": {
    "tool_name": "Bash",
    "tool_input": {
      "command": "rm -rf node_modules && npm install",
      "description": "Clean reinstall dependencies"
    },
    "permission_suggestions": [
      {
        "type": "addRules",
        "rules": [{"toolName": "Bash", "ruleContent": "npm install"}],
        "behavior": "allow",
        "destination": "localSettings"
      }
    ]
  }
}
```

**应显示：** 命令（`command`）+ 说明（`description`）

### 2.2 Write 文件

```json
{
  "context": {
    "tool_name": "Write",
    "tool_input": {
      "file_path": "/project/src/config.json",
      "content": "{\"debug\": true}"
    }
  }
}
```

**应显示：** 文件路径（`file_path`）+ 内容（`content`）

### 2.3 Edit 文件

```json
{
  "context": {
    "tool_name": "Edit",
    "tool_input": {
      "file_path": "/project/src/main.py",
      "old_string": "print('hello')",
      "new_string": "print('hello world')"
    }
  }
}
```

**应显示：** 文件路径 + 旧内容 → 新内容

### 2.4 MCP 工具

```json
{
  "context": {
    "tool_name": "mcp__github__create_pull_request",
    "tool_input": {
      "repo": "owner/repo",
      "title": "Fix bug",
      "body": "This PR fixes the issue"
    }
  }
}
```

**应显示：** 工具名 + tool_input 各字段

### 2.5 单选题（AskUserQuestion）

```json
{
  "context": {
    "tool_name": "AskUserQuestion",
    "tool_input": {
      "questions": [
        {
          "question": "你希望使用哪个包管理器？",
          "header": "包管理器选择",
          "options": [
            {"label": "npm"},
            {"label": "yarn"},
            {"label": "pnpm"},
            {"label": "bun"}
          ],
          "multiSelect": false
        }
      ]
    }
  }
}
```

**应显示：** 问题标题 + 单选列表，用户选择一个选项

**响应格式（见第 3.3 节）：** 需要用 `updatedInput` 回传完整 questions + answers

### 2.6 多选题（AskUserQuestion）

```json
{
  "context": {
    "tool_name": "AskUserQuestion",
    "tool_input": {
      "questions": [
        {
          "question": "需要哪些功能模块？",
          "header": "功能模块",
          "options": [
            {"label": "用户认证"},
            {"label": "数据库 ORM"},
            {"label": "API 路由"},
            {"label": "日志系统"}
          ],
          "multiSelect": true
        }
      ]
    }
  }
}
```

**应显示：** 问题标题 + 多选列表，用户可选多个选项

**响应格式（见第 3.3 节）：** answers 的值为数组

---

## 3. 响应格式（设备 → Bridge → Hook → Claude Code）

设备端直接发送 Claude Code 协议格式的 decision 对象，bridge 透传给 hook，hook 包装后输出到 stdout。

### 3.1 允许

```json
{
  "behavior": "allow"
}
```

### 3.2 允许并记住规则

```json
{
  "behavior": "allow",
  "updatedPermissions": [
    {
      "type": "addRules",
      "rules": [{"toolName": "Bash", "ruleContent": "npm install"}],
      "behavior": "allow",
      "destination": "localSettings"
    }
  ]
}
```

### 3.3 回答问题（AskUserQuestion）

单选 — answers 值为字符串：

```json
{
  "behavior": "allow",
  "updatedInput": {
    "questions": [
      {
        "question": "你希望使用哪个包管理器？",
        "header": "包管理器选择",
        "options": [{"label": "npm"}, {"label": "yarn"}, {"label": "pnpm"}],
        "multiSelect": false
      }
    ],
    "answers": {
      "你希望使用哪个包管理器？": "pnpm"
    }
  }
}
```

多选 — answers 值为数组：

```json
{
  "behavior": "allow",
  "updatedInput": {
    "questions": [
      {
        "question": "需要哪些功能模块？",
        "header": "功能模块",
        "options": [{"label": "用户认证"}, {"label": "数据库 ORM"}, {"label": "API 路由"}],
        "multiSelect": true
      }
    ],
    "answers": {
      "需要哪些功能模块？": ["用户认证", "API 路由"]
    }
  }
}
```

> `answers` 的 key 是 `question` 字段原文，value 是选中的 `label`。

### 3.4 拒绝

```json
{
  "behavior": "deny",
  "message": "用户拒绝了此操作"
}
```

`message` 可选，Claude 会收到并据此调整行为。

### 3.5 拒绝并中断

```json
{
  "behavior": "deny",
  "message": "危险操作，已中断",
  "interrupt": true
}
```

`interrupt: true` 会完全停止 Claude 当前执行。

---

## 4. 权限规则格式（permission_suggestions / updatedPermissions）

### 4.1 规则条目类型

| type | 说明 |
|------|------|
| `addRules` | 添加权限规则 |
| `replaceRules` | 替换指定 scope 的全部规则 |
| `removeRules` | 移除匹配的规则 |
| `setMode` | 设置权限模式 |
| `addDirectories` | 添加受信任目录 |
| `removeDirectories` | 移除受信任目录 |

### 4.2 rules 对象

```json
{"toolName": "Bash", "ruleContent": "npm install"}
```

- `toolName`: 工具名（`Bash`、`Write`、`Edit` 等，省略则匹配整个工具）
- `ruleContent`: 匹配模式（可选，支持 glob）

### 4.3 destination 持久化范围

| destination | 存储位置 | 生命周期 |
|-------------|----------|----------|
| `session` | 仅内存 | 会话结束即失效 |
| `localSettings` | `.claude/settings.local.json` | 本地持久化，不提交 git |
| `projectSettings` | `.claude/settings.json` | 团队共享 |
| `userSettings` | `~/.claude/settings.json` | 全局所有项目 |

### 4.4 setMode 可用模式

| mode | 说明 |
|------|------|
| `default` | 默认模式 |
| `acceptEdits` | 自动接受文件编辑 |
| `dontAsk` | 不再询问（按规则处理） |
| `bypassPermissions` | 绕过所有权限检查 |

---

## 5. 传输协议

### 5.1 TCP 设备

设备发送 JSON lines（换行分隔），bridge 透传 decision：

```
→ {"cmd":"permission","id":"req-uuid","behavior":"allow","updatedPermissions":[...]}
← {"cmd":"permission_done","id":"req-uuid","decision":"allow"}
```

bridge 剥离路由字段（`cmd`、`id`），其余原样转发给 hook。

### 5.2 Web 页面

POST `/api/decide`，bridge 透传 decision：

```json
{
  "session_id": "...",
  "id": "req-uuid",
  "behavior": "allow",
  "updatedPermissions": [...]
}
```

bridge 剥离路由字段（`session_id`、`id`），其余原样转发给 hook。

### 5.3 Hook 包装

hook 收到 bridge 的 decision 对象后，包装为 Claude Code 要求的格式输出到 stdout：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": { /* bridge 透传的 decision 对象 */ }
  }
}
```

---

## 6. 审批流程时序

```
Claude Code         Hook              Bridge            设备/Web
    │                 │                 │                  │
    │ PermissionReq   │                 │                  │
    │ (stdin JSON)    │                 │                  │
    │────────────────>│                 │                  │
    │                 │  TCP JSON line  │                  │
    │                 │ (透传 context)  │                  │
    │                 │────────────────>│                  │
    │                 │                 │  广播请求         │
    │                 │                 │─────────────────>│
    │                 │                 │                  │ 用户操作
    │                 │                 │  decision 对象    │
    │                 │                 │<─────────────────│
    │                 │  decision 对象  │                  │
    │                 │<────────────────│                  │
    │                 │ 包装输出         │                  │
    │ stdout JSON     │                 │                  │
    │<────────────────│                 │                  │
    │                 │                 │                  │
    │                 │                 │  广播 done        │
    │                 │                 │─────────────────>│
```
