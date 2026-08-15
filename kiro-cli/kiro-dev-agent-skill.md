# Kiro Dev Agent Skill：让 Hermes 拥有编码子 Agent

> 将 Kiro CLI 注册为 Hermes Agent 的 skill，实现"我思考规划，Kiro 动手写码"的分工模式

## 概述

`kiro-dev-agent` 是 Hermes Agent 的一个 skill，作用是将开发任务委托给 Kiro CLI 子 agent 执行。Hermes 负责理解需求、规划方案、验证结果；Kiro 负责写代码、创建文件、运行测试。

**核心价值：** 一行命令获得一个自主编码 agent，无需管理 API、SDK、模型配置。

```
用户 → Hermes (主 Agent: 理解、规划、验证)
                │
                ▼
        kiro-dev-agent skill 触发
                │
                ▼
        kiro-cli (子 Agent: 写码、建文件、跑测试)
                │
                ▼
        Hermes 验证结果 → 返回用户
```

## Skill 定义

```yaml
name: kiro-dev-agent
category: autonomous-ai-agents
description: Delegate coding and development tasks to Kiro CLI sub-agent.
version: 1.0.0
tags: [kiro, agent, coding, development, delegation]
```

## 触发条件

Skill 在以下场景自动加载：

- 用户要求写代码、创建项目、修改文件
- 需要调试和修复错误
- 重构现有代码
- 搭建项目脚手架
- 运行测试并修复失败用例
- 任何需要文件系统操作 + 编码智能的任务

## 调用方式

### 基本调用

```bash
KIRO_API_KEY="$KIRO_API_KEY" kiro-cli chat \
  --no-interactive \
  --trust-all-tools \
  --wrap=never \
  "TASK_DESCRIPTION"
```

### 带工作目录

```bash
cd /path/to/project && \
KIRO_API_KEY="$KIRO_API_KEY" kiro-cli chat \
  --no-interactive \
  --trust-all-tools \
  --wrap=never \
  "TASK_DESCRIPTION"
```

### 带上下文注入（复杂任务）

```bash
kiro-cli chat --no-interactive --trust-all-tools --wrap=never "
Project: FastAPI backend in /tmp/myproject
Existing files: main.py, models.py, database.py
Convention: Use pydantic v2, async handlers, Google-style docstrings

Task: Add a /users/{id}/profile endpoint that returns user profile with avatar URL
"
```

### 从 Hermes 调用

```python
# 方式 1: terminal 直接调用
terminal(
    command='KIRO_API_KEY="$KIRO_API_KEY" kiro-cli chat --no-interactive --trust-all-tools --wrap=never "YOUR_TASK"',
    workdir="/path/to/project",
    timeout=300
)

# 方式 2: delegate_task 委托
delegate_task(
    goal="Use kiro-cli to complete the development task",
    context="Command: kiro-cli chat --no-interactive --trust-all-tools --wrap=never 'TASK'\nProject dir: /path/to/project",
    toolsets=["terminal"]
)
```

## 参数说明

| Flag | 作用 | 必须 |
|------|------|------|
| `--no-interactive` | 非交互模式，不弹确认 | ✅ |
| `--trust-all-tools` | 自动批准所有工具调用 | ✅ |
| `--wrap=never` | 不换行，输出干净 | ✅ |
| `--agent NAME` | 使用特定 agent 配置 | 可选 |

## 前置条件

1. **kiro-cli 已安装**
   ```bash
   # 验证
   kiro-cli --version  # v2.18.0+
   ```

2. **认证已完成**
   ```bash
   # 方式 A: 环境变量 (推荐自动化场景)
   export KIRO_API_KEY="ksk_xxxxx"

   # 方式 B: 交互登录
   kiro-cli login

   # 验证
   kiro-cli whoami
   ```

## 实测验证

### 测试任务

> 创建一个 Python CLI 计算器：4 个运算函数 + CLI 入口 + 完整 pytest 测试

### 调用命令

```bash
cd /tmp/kiro-test-project && \
KIRO_API_KEY="$KIRO_API_KEY" kiro-cli chat --no-interactive --trust-all-tools --wrap=never "
Create a Python CLI calculator app with the following requirements:
1. File: calc.py
2. Functions: add, subtract, multiply, divide (handle division by zero)
3. A main() function that takes args from command line: python calc.py add 3 5
4. Include docstrings
5. Add a test file: test_calc.py with pytest tests for all functions including edge cases
"
```

### Kiro 自主执行过程

1. **创建 `calc.py`** (105 行)
   - 4 个函数 + 类型注解 + Google-style docstrings
   - `main()` 解析 CLI 参数，含输入验证和错误处理
   - 除零保护 (`ValueError`)

2. **创建 `test_calc.py`** (149 行)
   - `TestAdd` — 6 个测试（正数、负数、混合、零、浮点、大数）
   - `TestSubtract` — 5 个测试
   - `TestMultiply` — 6 个测试（含乘零、乘一）
   - `TestDivide` — 7 个测试（含除零异常、零分子、非整除）
   - `TestMainCLI` — 8 个集成测试（subprocess 调用 CLI）

3. **自动运行测试验证**
   ```
   ============================== 32 passed in 0.22s ==============================
   ```

### 执行结果

| 指标 | 值 |
|------|-----|
| 耗时 | 27 秒 |
| Credits | 0.38 |
| 生成文件 | 2 个 (calc.py + test_calc.py) |
| 代码行数 | 254 行 |
| 测试数量 | 32 个 |
| 测试通过 | 32/32 (100%) |

### 生成的代码

#### calc.py

```python
"""A simple command-line calculator supporting basic arithmetic operations."""

import sys


def add(a: float, b: float) -> float:
    """Return the sum of two numbers.

    Args:
        a: First operand.
        b: Second operand.

    Returns:
        The sum of a and b.
    """
    return a + b


def subtract(a: float, b: float) -> float:
    """Return the difference of two numbers.

    Args:
        a: First operand.
        b: Second operand.

    Returns:
        The result of a minus b.
    """
    return a - b


def multiply(a: float, b: float) -> float:
    """Return the product of two numbers.

    Args:
        a: First operand.
        b: Second operand.

    Returns:
        The product of a and b.
    """
    return a * b


def divide(a: float, b: float) -> float:
    """Return the quotient of two numbers.

    Args:
        a: Numerator.
        b: Denominator.

    Returns:
        The result of a divided by b.

    Raises:
        ValueError: If b is zero.
    """
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b


def main():
    """Parse command-line arguments and perform the requested operation.

    Usage:
        python calc.py <operation> <num1> <num2>

    Operations: add, subtract, multiply, divide
    """
    operations = {
        "add": add,
        "subtract": subtract,
        "multiply": multiply,
        "divide": divide,
    }

    if len(sys.argv) != 4:
        print(f"Usage: python calc.py <operation> <num1> <num2>")
        print(f"Operations: {', '.join(operations.keys())}")
        sys.exit(1)

    operation = sys.argv[1]
    if operation not in operations:
        print(f"Unknown operation: {operation}")
        print(f"Operations: {', '.join(operations.keys())}")
        sys.exit(1)

    try:
        num1 = float(sys.argv[2])
        num2 = float(sys.argv[3])
    except ValueError:
        print("Error: Both arguments must be valid numbers")
        sys.exit(1)

    try:
        result = operations[operation](num1, num2)
        print(result)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

#### test_calc.py (节选)

```python
"""Tests for the calc module."""

import subprocess
import sys
import pytest
from calc import add, subtract, multiply, divide


class TestAdd:
    def test_positive_numbers(self):
        assert add(3, 5) == 8

    def test_negative_numbers(self):
        assert add(-3, -5) == -8

    def test_floats(self):
        assert add(1.5, 2.5) == 4.0


class TestDivide:
    def test_division_by_zero(self):
        with pytest.raises(ValueError, match="Cannot divide by zero"):
            divide(10, 0)

    def test_non_integer_result(self):
        assert divide(1, 3) == pytest.approx(0.3333333333333333)


class TestMainCLI:
    """Integration tests for the CLI interface."""

    def _run_calc(self, *args):
        result = subprocess.run(
            [sys.executable, "calc.py", *args],
            capture_output=True, text=True,
        )
        return result

    def test_add(self):
        result = self._run_calc("add", "3", "5")
        assert result.returncode == 0
        assert result.stdout.strip() == "8.0"

    def test_divide_by_zero(self):
        result = self._run_calc("divide", "10", "0")
        assert result.returncode == 1
        assert "Cannot divide by zero" in result.stdout
```

## 注意事项

| 问题 | 说明 |
|------|------|
| **超时** | 复杂任务可能 2-5 分钟，设 `timeout=300` |
| **工作目录** | Kiro 在 cwd 操作，务必指定项目目录 |
| **无多轮** | 每次调用独立，所有上下文需在 prompt 中提供 |
| **输出冗长** | Kiro 会打印工具调用描述，解析最终结果即可 |
| **Credits 消耗** | 简单任务 ~0.03，中等 ~0.10，复杂 ~0.30+ |
| **文件验证** | 完成后应检查文件存在、运行测试 |

## 成本参考

| 任务复杂度 | 示例 | Credits | 耗时 |
|-----------|------|---------|------|
| 简单 | 写一个函数 | 0.02-0.03 | 5-10s |
| 中等 | 多文件 + 测试 | 0.05-0.15 | 15-30s |
| 复杂 | 项目脚手架 + 测试 + 验证 | 0.20-0.50 | 30-60s |
| 重型 | 全栈功能开发 | 0.50-1.00 | 1-3min |

## 与其他 Skill 的关系

| Skill | 适用场景 | 区别 |
|-------|---------|------|
| `kiro-dev-agent` | 开发任务委托 | Kiro 后端，含工具调用 |
| `claude-code` | Claude Code CLI 委托 | Anthropic 后端 |
| `codex` | OpenAI Codex CLI 委托 | OpenAI 后端 |
| `opencode` | OpenCode CLI 委托 | 开源方案 |

四个 coding agent skill 可根据场景选择，Kiro 的优势是 **一键订阅、零配置、含工具能力**。

## 参考链接

- [Kiro CLI 文档](https://docs.kiro.dev/cli/getting-started)
- [AWS Blog: Kiro CLI 作为 Agent SDK](https://aws.amazon.com/cn/blogs/china/use-kiro-cli-as-agent-sdk-build-your-agent-app-with-one-click-subscription/)
- [Hermes Skills 文档](https://github.com/hermes-agent/hermes)
