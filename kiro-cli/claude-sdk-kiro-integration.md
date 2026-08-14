# Claude Agent SDK + Kiro CLI 集成指南

## 概述

通过 Claude Code SDK（Python）编排层调用 Kiro CLI 作为子 Agent 执行开发任务。Claude 作为 orchestrator 通过 Bash tool 委托 kiro-cli 完成编码、文件操作、Shell 执行等工作。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│  Your Python Service                                     │
│                                                          │
│  claude-code-sdk (Python)                                │
│       │                                                  │
│       ▼                                                  │
│  Claude Agent (Orchestrator)                             │
│       │  model: claude-sonnet-4-6 via LiteLLM            │
│       │                                                  │
│       ▼  Bash tool                                       │
│  kiro-cli chat --no-interactive (Coding Agent)           │
│       │                                                  │
│       ├── stdout → 代码结果 + 执行输出                    │
│       └── "Credits: X.XX • Time: Xs" → 用量追踪          │
└─────────────────────────────────────────────────────────┘
```

## 前置条件

| 组件 | 版本 | 安装方式 |
|------|------|----------|
| Claude Code CLI | v2.1.232+ | `npm install -g @anthropic-ai/claude-code` |
| Claude Code SDK (Python) | 0.0.25+ | `pip install claude-code-sdk` |
| Kiro CLI | v2.18.0+ | `curl -fsSL https://cli.kiro.dev/install.sh \| bash` |
| LiteLLM (可选) | v1.92.0 | Docker 部署，用于路由到 Bedrock |
| Python | 3.12+ | 系统自带 |

## 认证配置

### Kiro CLI 认证

Kiro CLI 通过 API Key 认证（环境变量方式）：

```bash
# 获取 API Key: Kiro Dashboard → API Keys → Create key
export KIRO_API_KEY=ksk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 验证
kiro-cli whoami
# Output: Authenticated with API key / Email: xxx@xxx.com
```

> ⚠️ API Key 创建后只显示一次，请妥善保存。

### Claude Code SDK 认证

指向 LiteLLM Proxy（或直接 Anthropic API）：

```bash
# 方式一：通过 LiteLLM Proxy（推荐，支持多模型路由）
export ANTHROPIC_BASE_URL=http://localhost:9191
export ANTHROPIC_API_KEY=<litellm-master-key>

# 方式二：直接 Anthropic API
export ANTHROPIC_API_KEY=sk-ant-xxxxx
```

## 快速开始

### 最简示例

```python
#!/usr/bin/env python3
import asyncio
import os
from claude_code_sdk import query, ClaudeCodeOptions

os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:9191"
os.environ["ANTHROPIC_API_KEY"] = "<your-litellm-key>"

KIRO_API_KEY = "<your-kiro-api-key>"

async def main():
    prompt = f"""Use the Bash tool to run:
KIRO_API_KEY={KIRO_API_KEY} kiro-cli chat --no-interactive --trust-all-tools "Create a hello world Python script at /tmp/hello.py and run it"

Report what Kiro did and the credits consumed."""

    async for msg in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            max_turns=5,
            allowed_tools=["Bash", "Read"],
            model="claude-sonnet-4-6"
        )
    ):
        # 提取文本响应
        if hasattr(msg, 'content') and isinstance(msg.content, list):
            for block in msg.content:
                if hasattr(block, 'text'):
                    print(block.text)

asyncio.run(main())
```

### 生产级封装

```python
#!/usr/bin/env python3
"""Production wrapper: Claude SDK orchestrator + Kiro CLI sub-agent"""

import asyncio
import os
import re
from dataclasses import dataclass
from claude_code_sdk import query, ClaudeCodeOptions


@dataclass
class KiroResult:
    """Kiro CLI execution result"""
    success: bool
    output: str
    credits: float
    time_seconds: float
    error: str = ""


async def call_kiro(
    task: str,
    kiro_api_key: str,
    working_dir: str = "/tmp",
    model: str = "claude-sonnet-4-6",
    max_turns: int = 5,
    trust_all: bool = True,
) -> KiroResult:
    """
    Orchestrate Claude → Kiro CLI to execute a development task.
    
    Args:
        task: Natural language description of the coding task
        kiro_api_key: Kiro API key (ksk_xxx)
        working_dir: Working directory for kiro-cli
        model: Claude model for orchestration
        max_turns: Max orchestrator turns
        trust_all: Whether to auto-approve all Kiro tools
    
    Returns:
        KiroResult with output, credits consumed, and timing
    """
    trust_flag = "--trust-all-tools" if trust_all else ""
    
    prompt = f"""You are an orchestrator agent. Execute the following task using Kiro CLI.

Run this command:
cd {working_dir} && KIRO_API_KEY={kiro_api_key} kiro-cli chat --no-interactive {trust_flag} --wrap=never "{task}"

After completion, extract and report in this exact format:
KIRO_OUTPUT: <what kiro accomplished>
KIRO_CREDITS: <number from "Credits: X.XX" line>
KIRO_TIME: <number from "Time: Xs" line>
KIRO_SUCCESS: true/false
"""

    text_blocks = []
    async for msg in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            max_turns=max_turns,
            allowed_tools=["Bash", "Read"],
            model=model,
        )
    ):
        if hasattr(msg, 'content') and isinstance(msg.content, list):
            for block in msg.content:
                if hasattr(block, 'text'):
                    text_blocks.append(block.text)

    full_output = "\n".join(text_blocks)
    
    # Parse structured output
    credits = 0.0
    time_s = 0.0
    success = True
    
    credits_match = re.search(r"KIRO_CREDITS:\s*([\d.]+)", full_output)
    if credits_match:
        credits = float(credits_match.group(1))
    
    time_match = re.search(r"KIRO_TIME:\s*([\d.]+)", full_output)
    if time_match:
        time_s = float(time_match.group(1))
    
    success_match = re.search(r"KIRO_SUCCESS:\s*(true|false)", full_output, re.I)
    if success_match:
        success = success_match.group(1).lower() == "true"

    return KiroResult(
        success=success,
        output=full_output,
        credits=credits,
        time_seconds=time_s,
    )


# === Usage Example ===
async def main():
    result = await call_kiro(
        task="Create a FastAPI app at /tmp/app.py with a /health endpoint returning JSON {status: ok}. Then run pytest if tests exist.",
        kiro_api_key=os.environ.get("KIRO_API_KEY", ""),
        working_dir="/tmp",
    )
    
    print(f"Success: {result.success}")
    print(f"Credits: {result.credits}")
    print(f"Time: {result.time_seconds}s")
    print(f"Output:\n{result.output}")


if __name__ == "__main__":
    asyncio.run(main())
```

## Kiro CLI 参数参考

```bash
kiro-cli chat [OPTIONS] [INPUT]

# 关键参数
--no-interactive          # 非交互模式（必须，用于子进程调用）
--trust-all-tools / -a    # 自动批准所有工具调用
--trust-tools <TOOLS>     # 仅信任指定工具（如 fs_read,fs_write,shell）
--model <MODEL>           # 指定模型
--effort <EFFORT>         # 推理力度: low/medium/high/xhigh/max
--agent-engine <ENGINE>   # Agent 引擎: v1/v2/v3
--wrap=never              # 禁止自动换行（方便解析输出）
--v3                      # 使用 V3 引擎（支持 cloud sandbox）
--cloud                   # 在云端沙箱执行（V3 only）
```

## Kiro CLI 工具能力

Kiro 内置以下工具（通过 `--trust-all-tools` 全部启用）：

| 工具 | 功能 |
|------|------|
| `read` | 读取文件/目录 |
| `write` | 创建/修改文件 |
| `shell` | 执行 Shell 命令 |
| `search` | 搜索文件内容 |
| `replace` | 查找替换 |
| MCP servers | 可配置外部 MCP 工具 |

## 费用追踪

Kiro CLI 每次调用在输出末尾附带用量信息：

```
▸ Credits: 0.11 • Time: 8s
```

- **Credits** 是 Kiro 的计费单位
- 可在 [AWS Console → Kiro Dashboard](https://us-east-1.console.aws.amazon.com/amazonq/developer/home#/kiro/dashboard) 查看汇总
- Dashboard 支持按 Client Type (CLI/IDE/WEB) 分维度查看

## 与 ACP 协议的对比

| 方案 | 状态 | 说明 |
|------|------|------|
| **Bash 调用（推荐）** | ✅ 已验证 | `kiro-cli chat --no-interactive` |
| ACP (JSON-RPC) | ⚠️ 协议未完全公开 | `kiro-cli acp` 可建立连接，但方法名未知 |
| Claude SDK spawnProcess | ❌ 协议不匹配 | SDK 期望 stream-json，kiro 说 JSON-RPC |

### ACP 探索记录

`kiro-cli acp` 使用 JSON Lines 格式（非 Content-Length 帧），`initialize` 方法可正常握手：

```json
// 请求
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-09-01","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}

// 响应
{"jsonrpc":"2.0","result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"promptCapabilities":{"image":true}},"agentInfo":{"name":"Kiro CLI Agent","version":"2.18.0"}},"id":1}
```

但后续方法（`conversation/turn`, `tasks/create`, `agent/run` 等）均返回 `-32601 Method not found`。待 Kiro ACP 协议公开后可切换。

## 环境部署清单

```bash
# 1. 安装 Claude Code CLI
npm install -g @anthropic-ai/claude-code

# 2. 安装 Python SDK
pip install claude-code-sdk

# 3. 安装 Kiro CLI
curl -fsSL https://cli.kiro.dev/install.sh | bash

# 4. 配置环境变量
export KIRO_API_KEY=ksk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export ANTHROPIC_BASE_URL=http://localhost:9191  # LiteLLM
export ANTHROPIC_API_KEY=sk-xxxxx               # LiteLLM master key

# 5. 验证
kiro-cli whoami                    # → Authenticated with API key
claude --version                   # → v2.1.232+
python3 -c "import claude_code_sdk; print('OK')"
```

## 实测数据

| 任务 | Credits | 时间 | 工具调用 |
|------|---------|------|----------|
| Say hello (1句话) | 0.03 | 2s | 0 |
| 读取目录 + 判断 OS | 0.08 | 6s | 1 (read) |
| 创建文件 + 运行 | 0.10 | 8s | 2 (write + shell) |
| Fibonacci 函数 + 测试 | 0.11 | 8s | 2 (write + shell) |

## 注意事项

1. **`--no-interactive` 是必须的** — 否则 kiro-cli 等待用户输入会 hang
2. **`--trust-all-tools` 的安全风险** — Kiro 会不经确认执行任何命令，生产环境建议用 `--trust-tools` 白名单
3. **API Key 24h 生效** — 新建 Kiro 用户后可能需等待
4. **Python 版本** — claude-code-sdk 需要 Python 3.12+
5. **Claude Code CLI 必须存在** — SDK 内部 spawn `claude` binary
6. **Credits 按使用量计费** — 复杂任务（多轮工具调用）消耗更多
