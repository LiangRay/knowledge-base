# Claude Agent SDK 编排 Kiro CLI 子 Agent：方案选型与实现

> 主 Agent (Claude) 委托开发任务给 Kiro CLI 子 Agent 的最佳实践

## 架构目标

```
用户请求
  │
  ▼
┌───────────────────────────────────────┐
│  主 Agent (Claude Code SDK / LiteLLM)  │  ← 通用推理、规划、问答
│                                       │
│  路由判断：是开发任务？                  │
│    YES → 委托 Kiro CLI 子 Agent        │  ← 写代码、改文件、调试
│    NO  → Claude 自己处理               │  ← 分析、总结、建议
└───────────────────────────────────────┘
```

## 两种调用方式对比

### ACP 模式 (kiro-cli acp)

长驻子进程，通过 JSON-RPC 2.0 over stdio 通信。

```
App ←→ kiro-cli acp (常驻进程)
        │
        ├─ initialize (握手)
        ├─ session/new (建会话)
        ├─ session/prompt (发任务) ←→ 流式通知
        ├─ session/prompt (第二轮)
        └─ [close stdin → 退出]
```

### Bash Tool 模式 (chat --no-interactive)

每次调用启动新进程，完成即退出。

```
App → subprocess("kiro-cli chat --no-interactive ...") → stdout → 结束
```

### 详细对比

| 维度 | ACP | Bash Tool |
|------|-----|-----------|
| 实现复杂度 | **高** — JSON-RPC、session 管理、流式解析 | **低** — 一行 subprocess |
| 启动开销 | 首次慢（握手+建会话），后续复用快 | 每次冷启动 |
| 多轮上下文 | ✅ 天然支持，同一 sessionId | ❌ 每次独立，无记忆 |
| 流式输出 | ✅ 逐 token 推送 | ❌ 等全部完成才返回 |
| 工具调用可见性 | ✅ 能看到每个 tool_call 事件 | ❌ 只看最终输出 |
| 超时/取消 | 精细 — session/cancel | 粗暴 — kill 进程 |
| 稳定性 | 长驻进程有崩溃风险，需重连逻辑 | 无状态，天然容错 |
| 调试难度 | 高 — 协议层问题难排查 | 低 — stdout/stderr 直观 |
| 适用场景 | IDE 插件、Chat UI、长会话产品 | Agent 编排、一次性任务委托 |

### 选型结论

| 场景 | 推荐方式 | 原因 |
|------|---------|------|
| **Agent 编排（本项目）** | ✅ Bash Tool | 任务是一次性的，主 Agent 已有上下文，简单可靠 |
| IDE / Chat UI 产品 | ACP | 需要流式显示、多轮对话、精细控制 |
| 自动化 CI/CD | Bash Tool | 无状态、易集成、可超时重试 |
| 交互式开发助手 | ACP | 持续对话、上下文保持 |

## 推荐方案：Bash Tool 编排

### 核心实现

```python
#!/usr/bin/env python3
"""
Orchestrator: Claude 主 Agent + Kiro 子 Agent (Bash Tool 模式)
"""
import asyncio
import subprocess
import os
import sys
import json
from typing import Optional

# ─── Config ──────────────────────────────────────────────────────────

KIRO_API_KEY = os.environ.get("KIRO_API_KEY", "")
LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://localhost:9191/v1")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"
PROJECT_DIR = os.environ.get("PROJECT_DIR", "/tmp/project")

# ─── Router ──────────────────────────────────────────────────────────

DEV_KEYWORDS = [
    # 中文
    "写代码", "写一个", "实现", "创建文件", "修改文件", "编写", "开发",
    "修复", "重构", "添加功能", "新建", "搭建", "生成代码",
    # English
    "write code", "create a", "implement", "build", "fix", "refactor",
    "generate", "scaffold", "set up", "debug", "write a", "make a",
]


def is_dev_task(prompt: str) -> bool:
    """Simple keyword-based router. Replace with LLM classifier for production."""
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in DEV_KEYWORDS)


# ─── Kiro Sub-Agent (Bash Tool) ─────────────────────────────────────

def kiro_execute(task: str, cwd: str = PROJECT_DIR, timeout: int = 300) -> dict:
    """
    Delegate a dev task to Kiro CLI.
    
    Returns:
        {"success": bool, "output": str, "credits": float}
    """
    env = {**os.environ, "KIRO_API_KEY": KIRO_API_KEY}

    try:
        result = subprocess.run(
            [
                "kiro-cli", "chat",
                "--no-interactive",
                "--trust-all-tools",
                "--wrap=never",
                task,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=env,
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        # Extract credits from stderr if present
        credits = 0.0
        if "Credits used:" in stderr:
            try:
                credits = float(stderr.split("Credits used:")[1].split()[0])
            except (IndexError, ValueError):
                pass

        return {
            "success": result.returncode == 0,
            "output": output,
            "stderr": stderr if result.returncode != 0 else "",
            "credits": credits,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "stderr": f"Timeout after {timeout}s", "credits": 0}
    except FileNotFoundError:
        return {"success": False, "output": "", "stderr": "kiro-cli not found", "credits": 0}


# ─── Claude Main Agent (via LiteLLM) ────────────────────────────────

async def claude_respond(prompt: str, system: str = None) -> str:
    """
    Call Claude via LiteLLM for non-dev tasks.
    Falls back to Kiro if LiteLLM unavailable.
    """
    import httpx

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{LITELLM_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                json={
                    "model": CLAUDE_MODEL,
                    "messages": messages,
                    "max_tokens": 2048,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    except Exception as e:
        # Fallback: use kiro for general Q&A too
        result = kiro_execute(
            f"[Do NOT use tools. Just answer directly.]\n\n{prompt}",
            cwd="/tmp",
        )
        return result["output"] if result["success"] else f"Error: {e}"


# ─── Orchestrator ────────────────────────────────────────────────────

class Orchestrator:
    """Routes user requests between Claude (reasoning) and Kiro (coding)."""

    def __init__(self, project_dir: str = PROJECT_DIR):
        self.project_dir = project_dir
        os.makedirs(project_dir, exist_ok=True)

    async def handle(self, user_input: str) -> str:
        """Main entry point."""
        if is_dev_task(user_input):
            return self._handle_dev(user_input)
        else:
            return await self._handle_general(user_input)

    def _handle_dev(self, task: str) -> str:
        """Dev task → Kiro CLI subprocess."""
        print(f"🛠️  [Router] → Kiro (dev task)")
        print("-" * 50)

        result = kiro_execute(task, cwd=self.project_dir)

        if result["success"]:
            print(result["output"])
            if result["credits"]:
                print(f"\n📊 Credits: {result['credits']}")
            return result["output"]
        else:
            error_msg = f"❌ Kiro failed: {result['stderr']}"
            print(error_msg)
            return error_msg

    async def _handle_general(self, prompt: str) -> str:
        """General task → Claude via LiteLLM."""
        print(f"🧠 [Router] → Claude (general task)")
        print("-" * 50)

        response = await claude_respond(prompt)
        print(response)
        return response


# ─── Interactive Mode ────────────────────────────────────────────────

async def interactive():
    """REPL mode for continuous interaction."""
    print("=" * 60)
    print("🤖 Orchestrator: Claude (reasoning) + Kiro (coding)")
    print(f"📁 Project: {PROJECT_DIR}")
    print("   Type 'quit' to exit")
    print("=" * 60)

    orch = Orchestrator()

    while True:
        try:
            user_input = input("\n📥 You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            break

        print()
        await orch.handle(user_input)


# ─── Main ────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        # No args → interactive mode
        await interactive()
    else:
        # Single command mode
        user_input = " ".join(sys.argv[1:])
        print(f"📥 Input: {user_input}\n")
        orch = Orchestrator()
        await orch.handle(user_input)
        print(f"\n{'='*50}\n✅ Done")


if __name__ == "__main__":
    asyncio.run(main())
```

### 运行方式

```bash
# 环境变量
export KIRO_API_KEY="ksk_xxxxx"
export LITELLM_BASE="http://localhost:9191/v1"
export LITELLM_KEY="sk-xxxxx"
export PROJECT_DIR="/path/to/your/project"

# 单次调用
python3 orchestrator.py "写一个 FastAPI hello world"
python3 orchestrator.py "解释微服务架构"

# 交互模式
python3 orchestrator.py
```

### 测试验证

```bash
# Dev task → Kiro
$ python3 orchestrator.py "创建文件 hello.py，实现 hello world"
🛠️  [Router] → Kiro (dev task)
--------------------------------------------------
已创建 hello.py。运行方式：python hello.py

# General task → Claude
$ python3 orchestrator.py "解释什么是ACP协议"
🧠 [Router] → Claude (general task)
--------------------------------------------------
ACP (Agent Client Protocol) 是一个标准化 AI Agent 通信的开放协议...
```

## 进阶：生产环境改进

### 1. LLM 路由替代关键词匹配

```python
async def smart_router(prompt: str) -> str:
    """Use Claude to classify intent."""
    classification = await claude_respond(
        prompt,
        system="""Classify the user's request into exactly one category:
- "dev": coding, file creation/modification, debugging, deployment
- "general": explanation, analysis, planning, Q&A

Reply with ONLY the category name, nothing else."""
    )
    return classification.strip().lower()
```

### 2. 带重试的 Kiro 调用

```python
def kiro_execute_with_retry(task: str, max_retries: int = 2, **kwargs) -> dict:
    """Retry on transient failures."""
    for attempt in range(max_retries + 1):
        result = kiro_execute(task, **kwargs)
        if result["success"]:
            return result
        if "timeout" in result["stderr"].lower() and attempt < max_retries:
            continue  # Retry on timeout
        break
    return result
```

### 3. 上下文注入

```python
def kiro_execute_with_context(task: str, context: str = "", **kwargs) -> dict:
    """Inject project context into the task prompt."""
    full_prompt = task
    if context:
        full_prompt = f"""Project context:
{context}

Task:
{task}"""
    return kiro_execute(full_prompt, **kwargs)
```

### 4. 结果验证

```python
async def verified_dev_task(task: str, orch: Orchestrator) -> str:
    """Execute dev task, then verify the result with Claude."""
    # Step 1: Kiro executes
    result = kiro_execute(task, cwd=orch.project_dir)
    
    if not result["success"]:
        return f"Failed: {result['stderr']}"
    
    # Step 2: Claude reviews
    review = await claude_respond(
        f"The following task was completed:\nTask: {task}\nOutput: {result['output']}\n\nIs this correct and complete? Any issues?"
    )
    
    return f"Result: {result['output']}\n\nReview: {review}"
```

## ACP 模式参考（备选）

如果未来需要升级到 ACP（如做 IDE 产品），核心协议流程：

```python
# 1. 启动长驻进程
proc = subprocess.Popen(["kiro-cli", "acp", "--trust-all-tools"], ...)

# 2. 握手
send({"method": "initialize", "params": {"protocolVersion": 1, ...}})

# 3. 建会话（cwd 必传！）
send({"method": "session/new", "params": {"cwd": "/project", "mcpServers": []}})
# → 返回 sessionId

# 4. 发消息（流式接收）
send({"method": "session/prompt", "params": {
    "sessionId": "uuid", 
    "prompt": [{"type": "text", "text": "task"}]
}})
# → 收到 session/update 通知 (agent_message_chunk)
# → 最终收到 result (stopReason: "end_turn")

# 5. 多轮对话（复用 sessionId）
send({"method": "session/prompt", "params": {"sessionId": "same-uuid", ...}})
```

ACP 通知格式：
```json
{"method": "session/update", "params": {
  "sessionId": "uuid",
  "update": {
    "sessionUpdate": "agent_message_chunk",
    "content": {"type": "text", "text": "Hello"}
  }
}}
```

## 文件清单

| 文件 | 用途 |
|------|------|
| `orchestrator.py` | 主编排器（Bash Tool 模式） |
| `kiro_acp.py` | 原生 ACP 客户端（参考实现） |
| `kiro-acp-integration.md` | ACP 协议完整文档 |

## 参考链接

- [AWS Blog: 把 Kiro CLI 当作 Agent SDK](https://aws.amazon.com/cn/blogs/china/use-kiro-cli-as-agent-sdk-build-your-agent-app-with-one-click-subscription/)
- [Agent Client Protocol (ACP)](https://github.com/anthropics/agent-client-protocol)
- [Claude Code SDK (Python)](https://pypi.org/project/claude-code-sdk/)
- [Kiro CLI 文档](https://docs.kiro.dev/cli/getting-started)
