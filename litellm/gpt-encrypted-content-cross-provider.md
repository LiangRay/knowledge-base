# GPT Reasoning Model 跨供应商路由：encrypted_content 问题与解决方案

> 日期：2026-09-16
> 背景：多供应商部署 GPT reasoning model 时，encrypted_content 导致 failover 失败
> 更新：2026-11-12 —— 修正加密边界与 affinity 行为描述；新增「方案五：Hook 丢弃加密 reasoning」及实测（见文末）

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

- 同一个 AWS Account 内跨 Region → ✅ 兼容（2026-11 实测：Bedrock **Runtime** `/openai/v1/responses`，us-east-1 签发的 encrypted_content 在 us-east-2 原样回放 200；Mantle 跨 Region 未单独复测）
- 跨 AWS Account → ❌ 解密失败（未实测，按 org-scoped 推断）
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
- **硬亲和**：如果该 deployment 不可用（cooldown / 不健康），直接返回 429/503，**不会** strip 后切到其他 deployment（v1.99.0 源码 `router_utils/pre_call_checks/encrypted_content_affinity_check.py`）

```yaml
router_settings:
  enable_pre_call_checks: true
  optional_pre_call_checks: ["encrypted_content_affinity"]
```

适用场景：必须使用 Responses API（如 Codex CLI 的 `additional_tools` 特性），能接受"原后端挂了这轮就失败"。需要真正 failover 见方案五。

### 方案三：Provider Affinity（Session Pinning）

同一个 conversation/session 锁定到同一个供应商，避免中途切换。

### 方案四：Fallback 时降级到非推理模式

- 主路径：reasoning model（GPT-5.6 + Responses API）
- Fallback：同模型但走 Chat Completions（丢失推理上下文但不报错）
- 或 fallback 到普通模型（GPT-4o、Claude Sonnet）

### 方案五：LiteLLM Hook 丢弃加密 reasoning（推荐用于多供应商 Responses API）

思路：在 LiteLLM 自定义 callback 里把 `input` 中 `type: "reasoning"`（带 `encrypted_content`）的 item 删掉再发给上游。上游只会看到明文消息 + function_call / function_call_output，自然不存在"解不开"的问题。

两种写法：

| | 5a 全扔 | 5b 软亲和 + 按需扔（guard） |
|---|---|---|
| Hook | `async_pre_call_hook`（路由前） | `async_filter_deployments` + `async_pre_call_deployment_hook`（选定 deployment 后、发送前） |
| 逻辑 | 无条件删掉所有 reasoning item | 借 LiteLLM 内置打标记下签发 deployment；优先路由回原 deployment（保留 reasoning）；原 deployment 不健康才放开路由，并只删"别家签发"的 reasoning |
| 代码量 | ~15 行 | ~140 行 |
| 解密错误 | 不会出现 | 不会出现 |
| 推理连续性 | **每轮都丢**，同后端也丢 | 同后端保留，仅切换时丢 |
| Prompt Cache | input 前缀变化，命中下降 | 基本不受影响（回原后端） |
| 与内置 affinity 区别 | — | 内置是硬亲和（429/503）；guard 是软亲和，可切换 |

代码：[`hooks/strip_all_reasoning.py`](hooks/strip_all_reasoning.py)、[`hooks/encrypted_reasoning_guard.py`](hooks/encrypted_reasoning_guard.py)

挂载方式（与已有 hook 一样，放到 `/app/` 下并在 `litellm_settings.callbacks` 里引用）：

```yaml
litellm_settings:
  callbacks:
    - "encrypted_reasoning_guard.encrypted_reasoning_guard_instance"
    # 或最简版：- "strip_all_reasoning.strip_all_instance"
```

guard 可选环境变量：
- `ERG_MODEL_GROUPS=gpt-5.6-sol,gpt-6-astra`：只对这些 model_name 生效（默认全部）
- `ERG_UNTAGGED_POLICY=keep|strip`：没有打标的历史 reasoning（hook 上线前的会话）怎么处理，默认 keep

注意：
- 只对 Responses API（`aresponses`）生效，Chat Completions 透传。
- 丢 reasoning 的代价是模型丢失上一轮思考过程，多轮工具调用时要重新推理；OpenAI 官方建议推理模型工具调用时回传 reasoning，所以优先用 5b。
- **同一 AWS 账号内的 Bedrock 多 Region 负载均衡实测能互解，不需要此 hook**；真正需要的是跨 OpenAI org / 跨 Azure resource / 跨 AWS 账号 / Bedrock 与 OpenAI 混用。

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
- AWS Account `<ACCOUNT_ID>`, IAM Role
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

# encrypted_content 跨 Region (同 AWS Account, Bedrock Runtime /openai/v1/responses) - ✅
# turn1 @us-east-1 → reasoning(enc 3641 chars) + function_call
[A 同Region 原样回放]            200
[B 跨Region(us-east-2) 原样回放] 200
[C 跨Region 丢整个reasoning item] 200（input tokens 141 → 122）
[D 跨Region 只删 encrypted_content 字段] 200
[E 同Region 丢整个reasoning item] 200
```

### 方案五实测（2026-11-12）

环境：独立 LiteLLM v1.99.0 容器；model group `gpt-lb` 两个 deployment（backend-A / backend-B），`simple-shuffle`、`num_retries: 0`。两个后端是模拟上游：转发到 Bedrock，但**只认自己签发的 encrypted_content**，别家签发的直接返回 400（模拟跨 org / 跨 resource 边界）。

**确定性回放**：拿到一轮 `reasoning + function_call` 后，带着它原样回放 8 次：

| 配置 | 落原后端 | 落另一后端 | 结果 |
|---|---|---|---|
| 无 hook | 4 × 200 | **4 × 400** `The encrypted content could not be verified ... organization_id did not match` | 一换后端就挂 |
| 5a 全扔 | 5 × 200 | 3 × 200 | ✅ 日志 8 次 `dropped 1` |
| 5b guard | 8 × 200（全部软亲和回原后端） | 0 | ✅ |

**Codex 风格多轮工具调用**（依次查 3 个城市天气，每轮回放全部 output）：

| 配置 | 结果 |
|---|---|
| 无 hook | 8 组对话 1~2 组中途 400（取决于是否发生跨后端回放） |
| 5b guard（非流式） | 8/8 成功 |
| 5b guard（流式） | 5/5 成功 |

未覆盖：原后端宕机时 guard 的切换路径（逻辑上走"放开路由 + 剥离"分支，待补测）。

## 参考资料

- [LiteLLM Responses API encrypted_content Incident Report](https://docs.litellm.ai/blog/responses-api-encrypted-content-incident)
- bedrock-runtime OpenAI 兼容层：`/openai/v1/chat/completions`、`/openai/v1/responses`
- bedrock-mantle Responses API：`/v1/responses`

复现脚本：[`hooks/test/`](hooks/test/)（`strict_upstream.py <port> <name>` 起模拟上游；`replay.py` 确定性回放；`multiturn.py [base] [n] [stream]` 多轮工具调用）。
