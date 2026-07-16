# Codex CLI 接入 Bedrock (通过 LiteLLM Proxy) 配置指南

> **适用版本**: Codex CLI v0.144.4+ / LiteLLM v1.92.0+ / Bedrock Mantle (GPT-5.x 系列)

## 问题背景

OpenAI Codex CLI 通过 LiteLLM Proxy 连接 Bedrock Mantle 时，会遇到 `400 Invalid 'input'` 错误。

**根因**: Codex CLI (v26.707+) 使用非标准的 `additional_tools` input item 类型来传递工具定义：

```json
{
  "input": [
    {"type": "additional_tools", "role": "developer", "tools": [...]},
    {"type": "message", "role": "user", "content": "..."}
  ]
}
```

Bedrock Mantle 的 Responses API 只接受标准 OpenAI schema（message/reasoning/function_call 等），不认识 `additional_tools`，直接返回 400。

**为什么直连 Bedrock 没问题？** Codex 内置的 `amazon-bedrock` provider 会在客户端自动做格式转换（IAM SigV4 签名 + 字段重写）。但通过自定义 provider（如 LiteLLM）走时，Codex 原样发送请求，LiteLLM 也是 passthrough，导致 Bedrock 收到非标准字段后报错。

## 解决方案

使用 `codex_additional_tools_flatten` 自定义 callback，在 LiteLLM 转发请求前：
1. 从 `input[]` 中提取 `type == "additional_tools"` 的 item
2. 将其中的 `tools` 合并到顶层 `tools` 参数
3. 从 `input[]` 中移除该 item

## 部署步骤

### 1. 放置 Callback 脚本

将 [codex_additional_tools_flatten.py](./codex_additional_tools_flatten.py) 放到 LiteLLM 容器可访问的路径。

**方式 A: Docker Volume 挂载**
```yaml
# docker-compose.yml
services:
  litellm:
    image: ghcr.io/berriai/litellm:v1.92.0
    volumes:
      - ./codex_additional_tools_flatten.py:/app/codex_additional_tools_flatten.py
    # ...
```

**方式 B: 自定义镜像**
```dockerfile
FROM ghcr.io/berriai/litellm:v1.92.0
COPY codex_additional_tools_flatten.py /app/
```

### 2. LiteLLM 配置

```yaml
# config.yaml
model_list:
  # GPT-5.6 通过 Bedrock Mantle (Bearer Token 方式)
  - model_name: gpt-5.6-sol
    litellm_params:
      model: openai/openai.gpt-5.6-sol
      api_base: https://bedrock-mantle.us-east-1.api.aws/openai/v1
      api_key: os.environ/AWS_BEARER_TOKEN_BEDROCK
    model_info:
      mode: responses
      base_model: openai/openai.gpt-5.6-sol
      max_tokens: 1050000
      max_input_tokens: 1050000
      max_output_tokens: 128000
      supports_reasoning: true

  # GPT-5.6 通过 Bedrock Mantle (IAM SigV4 方式，推荐)
  # - model_name: gpt-5.6-sol
  #   litellm_params:
  #     model: bedrock_mantle/openai.gpt-5.6-sol
  #     aws_region_name: us-east-1
  #   model_info:
  #     mode: responses
  #     ...

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  cooldown_time: 60

litellm_settings:
  request_timeout: 600
  callbacks:
    - "codex_additional_tools_flatten.codex_additional_tools_flatten_instance"

general_settings:
  master_key: "os.environ/LITELLM_MASTER_KEY"
```

**关键配置说明**:
- `model: openai/...` — 使用 openai provider 做 passthrough（Bearer Token 认证）
- `model: bedrock_mantle/...` — 使用 bedrock_mantle provider（IAM SigV4 自动签名，无需 Token）
- `mode: responses` — 告诉 LiteLLM 这是 Responses API 模型
- `callbacks` — 注册 flatten callback 实例

### 3. Codex CLI 客户端配置

```toml
# ~/.codex/config.toml
model_provider = "litellm"
model = "gpt-5.6-sol"

[model_providers.litellm]
name = "LiteLLM"
base_url = "http://<your-litellm-host>:9191/v1"
wire_api = "responses"

[model_providers.litellm.auth]
# 方式1: 从文件读取 API Key
command = "/usr/bin/cat"
args = ["/path/to/litellm.key"]
refresh_interval_ms = 0

# 方式2: 环境变量 (需 shell wrapper)
# command = "/bin/sh"
# args = ["-c", "echo $LITELLM_API_KEY"]
```

**多 Profile 配置** (推荐):

```toml
# ~/.codex/config.toml — 默认直连 Bedrock
model_provider = "amazon-bedrock"
model = "openai.gpt-5.6-sol"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"
```

```toml
# ~/.codex/litellm.config.toml — LiteLLM 代理
model_provider = "litellm"
model = "gpt-5.6-sol"

[model_providers.litellm]
name = "LiteLLM"
base_url = "http://localhost:9191/v1"
wire_api = "responses"
```

使用时切换：
```bash
codex              # 直连 Bedrock (IAM SigV4)
codex -p litellm   # 通过 LiteLLM 代理
```

### 4. 验证

```bash
# 快速测试
codex -p litellm -q "echo hello"

# 查看 LiteLLM 日志确认 callback 生效
docker logs litellm-litellm-1 2>&1 | grep "CodexAdditionalToolsFlatten"
# 预期输出: "removed additional_tools item(s), flattened N tool(s) into top-level tools"
```

## 架构图

```
┌─────────────┐     Responses API      ┌───────────────┐     Responses API     ┌─────────────────┐
│  Codex CLI  │ ───────────────────────▶│  LiteLLM      │ ────────────────────▶ │ Bedrock Mantle  │
│             │  (含 additional_tools)   │  + callback   │  (tools 已提升到顶层) │ (GPT-5.x)       │
└─────────────┘                         └───────────────┘                       └─────────────────┘
                                              │
                                              ▼
                                   codex_additional_tools_flatten
                                   ① 提取 additional_tools items
                                   ② 合并 tools → 顶层
                                   ③ 清理 input[]
```

## 常见问题

### Q: 为什么不用 `drop_params: true`？
`drop_params` 在 LiteLLM v1.92.0 对 `/v1/responses` 路由仍然不生效（只适用于 `/v1/chat/completions`）。需要自定义 callback 来做请求改写。

### Q: GPT-5.5 通过 LiteLLM 能用但 5.6 不行？
GPT-5.5 配置了多个 deployment，LiteLLM 重试时可能触发 responses→chat completions bridge 模式（该模式会正确处理 additional_tools）。GPT-5.6 如果只有单个 deployment 则不会触发重试，直接报错。部署 callback 后两者都能正常工作。

### Q: 直连 Bedrock vs LiteLLM 代理选哪个？
| 场景 | 推荐方式 |
|------|----------|
| 个人开发，有 AWS 凭证 | 直连 (`amazon-bedrock` provider) |
| 团队共享，统一管理 | LiteLLM 代理 + callback |
| 需要负载均衡/fallback | LiteLLM 代理 |
| 需要审计/限流 | LiteLLM 代理 |

## 参考

- [Codex CLI GitHub](https://github.com/openai/codex)
- [LiteLLM Bedrock Mantle 文档](https://docs.litellm.ai/docs/providers/bedrock_mantle)
- [原始 callback 来源: kk137/litellm-eks-public](https://github.com/kk137/litellm-eks-public)
- Codex GitHub Issue #32086 — `additional_tools` 兼容性问题
