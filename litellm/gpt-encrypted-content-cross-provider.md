# GPT Reasoning Model 跨供应商路由：encrypted_content 问题与解决方案

> 日期：2026-09-16
> 背景：多供应商部署 GPT reasoning model 时，encrypted_content 导致 failover 失败

## 问题描述

在多供应商（Multi-Provider）架构中部署 GPT-5.6 等推理模型时，一旦对话进入多轮推理状态，**不能在不同供应商之间切换**。切换后整个对话链断裂，无法 failover。

核心原因：**推理模型的 thinking 状态在各供应商之间不兼容**。

## encrypted_content 机制详解

### 什么是 encrypted_content

GPT-5.6 系列（sol/terra/luna）、GPT-6 Astra、o3/o4-mini 等推理模型，在 Responses API 调用时，会返回一个加密的推理状态 blob（`rsn_` 开头），称为 `encrypted_content`。

它的作用是：在 `store: false`（不服务端存储对话）的场景下，让客户端保管推理链状态，下一轮对话时原样传回，模型可以继续之前的推理链。

### 触发条件（必须全部满足）

| 条件 | 说明 |
|---|---|
| **Reasoning Model** | GPT-5.6 系列、GPT-6 Astra、o3/o4-mini 等带推理能力的模型 |
| **Responses API** | 请求走 `/v1/responses`（`input` 字段） |
| **`store: false`** | 明确要求不在服务端存储对话历史 |
| **多轮对话** | 需要把上一轮的 output 作为下一轮 input 传回 |

### 不触发的场景

| 场景 | 原因 |
|---|---|
| `store: true`（默认值） | 服务端保存了对话，用 `previous_response_id` 续接 |
| Chat Completions API | 该 API 没有 encrypted_content 机制 |
| 非推理模型（GPT-4o、Claude 等） | 没有 reasoning tokens |
| 单轮对话 | 没有续接需求 |

### 加密范围（org-scoped）

- 同一个 AWS Account 内跨 Region → ✅ 兼容（Bedrock Mantle 场景）
- 跨 AWS Account → ❌ 解密失败
- OpenAI Direct → Azure OpenAI → ❌ 不同 org
- Bedrock Mantle → OpenAI Direct → ❌ 不同加密域

## 为什么跨供应商 Failover 会坏

多轮对话中，Responses API + `store:false` 场景：

```
轮次1: Provider A 返回 encrypted_content (rsn_abc123...)
轮次2: 客户端传回 rsn_abc123... → Provider A 解密成功 → 继续推理 ✅
轮次3: Provider A 挂了 → failover 到 Provider B
        客户端传回 rsn_abc123... → Provider B 无法解密 → 400 错误 ❌
```

**结果：一家服务但凡出一点问题，都不能 failback。**

## 解决方案

### 方案一：bedrock-runtime Chat Completions（推荐）

使用 AWS Bedrock Runtime 的 **Converse API / OpenAI 兼容层**，走 Chat Completions 格式，从根本上**绕过 encrypted_content 机制**。

#### 原理

| 维度 | Bedrock Mantle (Responses API) | Bedrock Runtime (Converse API) |
|---|---|---|
| API 路径 | `/v1/responses` | Converse API 或 `/openai/v1/chat/completions` |
| 认证 | SigV4 或 Bearer Token | SigV4 或 Bearer Token |
| 请求格式 | `input` 字段 | `messages` 数组 |
| encrypted_content | ✅ 会返回（store:false 时） | ❌ 不存在 |
| 多轮上下文 | `previous_response_id` 或 encrypted_content | messages 数组明文传递 |
| 跨供应商 fallback | ❌ encrypted_content 不兼容 | ✅ messages 天然兼容 |

#### LiteLLM 配置示例

```yaml
# Bedrock Runtime 模式 - Chat Completions (Converse API)
- model_name: gpt-5.6-sol
  litellm_params:
    model: bedrock/global.openai.gpt-5.6-sol
    aws_region_name: us-east-1

- model_name: gpt-5.6-sol
  litellm_params:
    model: bedrock/global.openai.gpt-5.6-sol
    aws_region_name: us-east-2

- model_name: gpt-5.6-sol
  litellm_params:
    model: bedrock/global.openai.gpt-5.6-sol
    aws_region_name: us-west-2
```

#### 关键差异

- 模型名用 `global.` 前缀（cross-region inference profile）
- LiteLLM provider 用 `bedrock/`（不是 `bedrock_mantle/`）
- 三个 Region 可用：us-east-1、us-east-2、**us-west-2**（比 Mantle 多一个）
- GPT 模型 `max_output_tokens` 最小值为 16

#### 可用模型（bedrock-runtime，截至 2026-09）

| 模型 | us-east-1 | us-east-2 | us-west-2 |
|---|---|---|---|
| global.openai.gpt-5.6-sol | ✅ | ✅ | ✅ |
| global.openai.gpt-5.6-terra | ✅ | ✅ | ✅ |
| global.openai.gpt-5.6-luna | ✅ | ✅ | ✅ |
| global.openai.gpt-6-astra | ✅ | ✅ | ✅ |

### 方案二：LiteLLM encrypted_content_affinity

LiteLLM v1.99.0 引入了 `encrypted_content_affinity` 配置，在路由层做 pinning：

- 检测到响应中有 encrypted_content 时，给它编码上 deployment ID
- 后续请求带回 encrypted_content 时，路由器自动亲和到同一个 deployment
- 如果该 deployment 不可用，strip 掉 encrypted_content 后 fallback

适用场景：必须使用 Responses API（如 Codex CLI 的 `additional_tools` 特性），但又想要 failover 能力。

### 方案三：Provider Affinity（Session Pinning）

同一个 conversation/session 锁定到同一个供应商，避免中途切换。

### 方案四：Fallback 时降级到非推理模式

- 主路径：reasoning model（GPT-5.6 + Responses API）
- Fallback：同模型但走 Chat Completions（丢失推理上下文但不报错）
- 或 fallback 到普通模型（GPT-4o、Claude Sonnet）

## 三种缓存机制对比

| 机制 | Responses API | Chat Completions API | 说明 |
|---|---|---|---|
| **Prompt Cache**（前缀自动缓存） | ✅ | ✅ | 自动生效，prompt >1024 tokens 时缓存前缀，缓存命中的 token 打折计费 |
| **`previous_response_id`**（服务端对话存储） | ✅（store:true 时） | ❌ | 服务端保存完整对话，不需要重传 messages，最高效但绑定单一服务端 |
| **`encrypted_content`**（推理链状态传递） | ✅（store:false 时） | ❌ | 加密推理状态交给客户端保管，org-scoped，跨供应商不兼容 |

**结论**：Chat Completions API 并非不能用缓存，只是没有 `previous_response_id` 这种"服务端存对话"的极致优化。对大多数场景，**Prompt Cache 自动命中已经足够**，trade-off 换来的跨供应商 fallback 兼容性是值得的。

## 实测验证

### 环境

- LiteLLM v1.99.0 + Docker Compose
- AWS Account 992382589304, IAM Role
- Bedrock Runtime (Converse API) + Bedrock Mantle (Responses API)

### 测试结果

```bash
# Bedrock Runtime (Converse API) - 全部 ✅
✅ bedrock/global.openai.gpt-5.6-luna @ us-east-1
✅ bedrock/global.openai.gpt-5.6-luna @ us-east-2
✅ bedrock/global.openai.gpt-5.6-luna @ us-west-2
✅ bedrock/global.openai.gpt-5.6-terra @ us-east-1
✅ bedrock/global.openai.gpt-5.6-sol @ us-east-1
✅ bedrock/global.openai.gpt-5.6-sol @ us-west-2
✅ bedrock/global.openai.gpt-6-astra @ us-east-1

# encrypted_content 跨 Region (同 AWS Account) - ✅
# us-east-1 返回的 encrypted_content 在 us-east-1 后续请求中可复用
```

## 参考资料

- [LiteLLM Responses API encrypted_content Incident Report](https://docs.litellm.ai/blog/responses-api-encrypted-content-incident)
- bedrock-runtime OpenAI 兼容层：`/openai/v1/chat/completions`、`/openai/v1/responses`
- bedrock-mantle Responses API：`/v1/responses`
