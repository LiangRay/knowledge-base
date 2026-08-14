# Kiro CLI ACP 集成指南：Python SDK 通过 ACP 协议调用 Kiro Agent

> 基于 AWS 官方博客 [把 Kiro CLI 当作 Agent SDK](https://aws.amazon.com/cn/blogs/china/use-kiro-cli-as-agent-sdk-build-your-agent-app-with-one-click-subscription/) 实践验证

## 概述

Kiro CLI 实现了 [Agent Client Protocol (ACP)](https://github.com/anthropics/agent-client-protocol) —— 一个标准化 AI Agent 与客户端通信的开放协议。通过 ACP，任何能启动子进程的应用都可以将 Kiro CLI 作为 Agent 后端，无需直接管理 API Key 或关心模型调用细节。

**架构：**

```
你的应用 (Python/Rust/Node.js)
  └─ ACP Client (JSON-RPC 2.0 over stdio)
       └─ kiro-cli acp (子进程)
            └─ Kiro 后端 (Claude model + Tools)
```

## 前置条件

- Kiro CLI v2.18.0+（已安装并登录）
- 认证方式：`KIRO_API_KEY` 环境变量 或 `kiro-cli login` 交互登录

```bash
# 验证认证状态
export KIRO_API_KEY="ksk_xxxxx"
kiro-cli whoami
```

## ACP 协议详解

### 通信模式

- **传输层**：子进程 stdin/stdout
- **协议**：JSON-RPC 2.0（每行一条消息）
- **消息类型**：Request（带 id）、Response（带 id + result/error）、Notification（无 id）

### 协议流程

```
Client                          kiro-cli acp
  │                                  │
  │──── initialize ─────────────────▶│
  │◀─── result (capabilities) ──────│
  │                                  │
  │──── session/new ────────────────▶│
  │◀─── notification (subagent) ────│
  │◀─── result (sessionId) ─────────│
  │                                  │
  │──── session/prompt ─────────────▶│
  │◀─── notification (metadata) ────│
  │◀─── notification (text chunk) ──│
  │◀─── notification (text chunk) ──│
  │◀─── notification (usage) ───────│
  │◀─── result (stopReason) ────────│
  │                                  │
  │──── [close stdin] ──────────────▶│
  └────────────────────────────────────┘
```

### 方法一览

| 方法 | 用途 | 关键参数 |
|------|------|---------|
| `initialize` | 握手，交换能力声明 | `protocolVersion`, `clientInfo` |
| `session/new` | 创建新会话 | `cwd`（必须）, `mcpServers` |
| `session/load` | 恢复已有会话 | `sessionId`, `cwd` |
| `session/prompt` | 发送用户消息 | `sessionId`, `prompt` |
| `session/cancel` | 取消当前生成 | `sessionId` |
| `session/set_model` | 切换模型 | `sessionId`, `modelId` |
| `session/list` | 列出所有会话 | — |

### 通知格式（流式输出）

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "uuid",
    "update": {
      "sessionUpdate": "agent_message_chunk",
      "content": {"type": "text", "text": "Hello"}
    }
  }
}
```

`sessionUpdate` 类型：
- `agent_message_chunk` — 文本流式输出
- `tool_call` — 工具调用开始
- `tool_call_update` — 工具调用状态更新
- `usage_update` — Credit 消耗统计

## 方式一：原生 ACP 客户端（推荐，~50行）

最简洁的实现，无额外依赖：

```python
#!/usr/bin/env python3
"""
Kiro ACP Client - minimal working example.
Usage: python3 kiro_acp.py "your prompt here"
"""
import asyncio, json, os, sys


async def kiro_acp(prompt: str, cwd: str = "/tmp") -> str:
    """Send a prompt to Kiro via ACP, return the response text."""
    proc = await asyncio.create_subprocess_exec(
        "kiro-cli", "acp", "--trust-all-tools",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    msg_id = 0

    async def rpc(method, params=None, timeout=15):
        nonlocal msg_id
        msg_id += 1
        current_id = msg_id
        msg = {"jsonrpc": "2.0", "id": current_id, "method": method}
        if params:
            msg["params"] = params
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

        text_parts = []
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout)
            if not line:
                break
            data = json.loads(line.decode().strip())

            # Streaming text notification
            if data.get("method") == "session/update":
                update = data["params"]["update"]
                if update.get("sessionUpdate") == "agent_message_chunk":
                    content = update.get("content", {})
                    if isinstance(content, dict) and content.get("type") == "text":
                        text_parts.append(content["text"])
                continue

            # Skip other notifications
            if "id" not in data:
                continue

            # Our response
            if data.get("id") == current_id:
                return data.get("result"), "".join(text_parts)

        return None, "".join(text_parts)

    # Protocol flow
    await rpc("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {},
        "clientInfo": {"name": "kiro-acp-client", "version": "1.0"}
    })

    result, _ = await rpc("session/new", {"cwd": cwd, "mcpServers": []})
    session_id = result["sessionId"]

    _, response_text = await rpc("session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": prompt}]
    }, timeout=120)

    proc.stdin.close()
    await proc.wait()
    return response_text


if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Hello!"
    answer = asyncio.run(kiro_acp(prompt))
    print(answer)
```

**运行结果：**

```bash
$ python3 kiro_acp.py "2+2等于几？只回答数字"
4
```

## 方式二：Claude Code SDK + 自定义 ACP Transport

通过实现 `claude-code-sdk` 的 `Transport` 抽象类，统一调用接口，可在 claude/kiro 后端间无缝切换：

```python
#!/usr/bin/env python3
"""Claude Code SDK + Kiro CLI via custom ACP Transport."""
import asyncio, json, os, sys
from typing import Any, AsyncIterator
from claude_code_sdk import ClaudeCodeOptions
from claude_code_sdk._internal.transport import Transport


class KiroACPTransport(Transport):
    """Custom Transport: claude-code-sdk interface → kiro-cli ACP."""

    def __init__(self, prompt: str, options: ClaudeCodeOptions):
        self._prompt = prompt
        self._options = options
        self._process = None
        self._msg_id = 0
        self._ready = False
        self._session_id = None

    async def connect(self) -> None:
        """Start kiro-cli acp, do handshake + session creation."""
        cwd = str(self._options.cwd) if self._options.cwd else "/tmp"

        self._process = await asyncio.create_subprocess_exec(
            "kiro-cli", "acp", "--trust-all-tools",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Initialize handshake
        result = await self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "claude-code-sdk-kiro", "version": "1.0.0"}
        })
        if not result:
            raise RuntimeError("ACP initialize failed")

        # Create session
        result = await self._rpc("session/new", {"cwd": cwd, "mcpServers": []})
        if not result or "sessionId" not in result:
            raise RuntimeError(f"session/new failed: {result}")

        self._session_id = result["sessionId"]
        self._ready = True

    async def _rpc(self, method: str, params: dict = None, timeout: float = 15):
        """Send JSON-RPC, wait for response (skip notifications)."""
        self._msg_id += 1
        current_id = self._msg_id
        msg = {"jsonrpc": "2.0", "id": current_id, "method": method}
        if params:
            msg["params"] = params

        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()

        try:
            while True:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), timeout
                )
                if not line:
                    return None
                data = json.loads(line.decode().strip())
                if data.get("id") == current_id:
                    return data.get("result")
        except asyncio.TimeoutError:
            return None

    async def write(self, data: str) -> None:
        """Send prompt via session/prompt."""
        self._msg_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": "session/prompt",
            "params": {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": self._prompt}]
            }
        }
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield messages in claude-code-sdk compatible format."""
        prompt_id = self._msg_id

        try:
            while True:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(), 120
                )
                if not line:
                    break
                data = json.loads(line.decode().strip())

                # Streaming notification
                if data.get("method") == "session/update":
                    update = data["params"]["update"]
                    su = update.get("sessionUpdate", "")

                    if su == "agent_message_chunk":
                        content = update.get("content", {})
                        if isinstance(content, dict) and content.get("type") == "text":
                            yield {
                                "type": "assistant",
                                "message": {"type": "text", "text": content["text"]}
                            }
                    elif su == "tool_call":
                        yield {
                            "type": "assistant",
                            "message": {
                                "type": "tool_use",
                                "name": update.get("title", "tool"),
                                "status": update.get("status", "")
                            }
                        }
                    continue

                # Skip other notifications
                if "id" not in data:
                    continue

                # Final response
                if data.get("id") == prompt_id:
                    yield {
                        "type": "result",
                        "result": data.get("result", {}),
                        "subtype": "success"
                    }
                    break

        except asyncio.TimeoutError:
            yield {"type": "result", "result": {"stopReason": "timeout"}, "subtype": "error"}

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        if self._process:
            self._process.stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except asyncio.TimeoutError:
                self._process.terminate()


# ─── Usage ───────────────────────────────────────────────────────────

async def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Hello!"
    options = ClaudeCodeOptions(cwd="/tmp")
    transport = KiroACPTransport(prompt=prompt, options=options)

    await transport.connect()
    print(f"Session: {transport._session_id[:8]}...")

    await transport.write(prompt)

    full_text = []
    async for msg in transport.read_messages():
        if msg["type"] == "assistant" and msg["message"]["type"] == "text":
            full_text.append(msg["message"]["text"])
        elif msg["type"] == "result":
            break

    print(f"Response: {''.join(full_text)}")
    await transport.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**运行结果：**

```bash
$ python3 test_sdk_kiro_acp.py "3乘以7等于多少？只回答数字"
Session: 9cdd7d9a...
Response: 21
```

## 方式三：Bash 委托（非 ACP，简单场景）

对于不需要流式输出的简单任务，可直接调用 CLI：

```bash
export KIRO_API_KEY="ksk_xxxxx"
kiro-cli chat --no-interactive --trust-all-tools --wrap=never "你的任务描述"
```

Python 封装：

```python
import subprocess, os

def kiro_simple(prompt: str) -> str:
    result = subprocess.run(
        ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", "--wrap=never", prompt],
        capture_output=True, text=True,
        env={**os.environ, "KIRO_API_KEY": os.environ["KIRO_API_KEY"]}
    )
    return result.stdout.strip()
```

## 三种方式对比

| 特性 | 原生 ACP | SDK + Transport | Bash 委托 |
|------|----------|----------------|-----------|
| 流式输出 | ✅ | ✅ | ❌ |
| 多轮对话 | ✅ (sessionId) | ✅ | ❌ |
| 工具调用可见性 | ✅ | ✅ | 仅最终结果 |
| 会话恢复 | ✅ (session/load) | ✅ | ❌ |
| MCP 服务器扩展 | ✅ | ✅ | ❌ |
| 代码复杂度 | ~50行 | ~150行 | ~5行 |
| 额外依赖 | 无 | claude-code-sdk | 无 |
| 后端切换 | 仅 Kiro | Kiro/Claude 可切换 | 仅 Kiro |

## Agent 能力 (v2.18.0)

Initialize 返回的 capabilities：

```json
{
  "protocolVersion": 1,
  "agentCapabilities": {
    "loadSession": true,
    "promptCapabilities": {
      "image": true,
      "audio": false,
      "embeddedContext": false
    },
    "mcpCapabilities": {"http": true, "sse": false}
  },
  "agentInfo": {
    "name": "Kiro CLI Agent",
    "version": "2.18.0"
  }
}
```

可用 Agent 模式：
- `kiro_default` — 默认通用 Agent
- `kiro_planner` — 规划模式，将想法分解为实现计划
- `kiro_guide` — Kiro 使用指南

## 关键注意事项

1. **`session/new` 必须传 `cwd`** — 缺少此参数会导致进程静默退出
2. **通知中 `content` 是 object 不是 array** — `{"type":"text","text":"..."}` 而非 `[{...}]`
3. **每行一条 JSON** — 标准 JSON-RPC over stdio，用 `\n` 分隔
4. **Credit 消耗** — 每次调用约 0.02-0.03 credits（简单问答）
5. **会话持久化** — 数据存储在 `~/.kiro/sessions/cli/`，可通过 `session/load` 恢复

## 参考链接

- [AWS Blog: 把 Kiro CLI 当作 Agent SDK](https://aws.amazon.com/cn/blogs/china/use-kiro-cli-as-agent-sdk-build-your-agent-app-with-one-click-subscription/)
- [Agent Client Protocol (ACP) 规范](https://github.com/anthropics/agent-client-protocol)
- [Kiro CLI 安装文档](https://docs.kiro.dev/cli/getting-started)
- [Claude Code SDK (Python)](https://pypi.org/project/claude-code-sdk/)
