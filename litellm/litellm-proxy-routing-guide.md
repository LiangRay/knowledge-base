# LiteLLM Proxy 路由机制深度指南

> 整理时间：2026-07-09
> 基于 LiteLLM v1.82.3-stable 实测 + 源码分析

---

## 1. 部署架构

```
┌─────────────────────────────────────────────────┐
│  LiteLLM Proxy (Docker, Port 9191)              │
│  EC2: 35.160.26.229                             │
│  IAM Role: Admin (SigV4 自动签名)                │
├─────────────────────────────────────────────────┤
│  模型组:                                         │
│  ├── claude-sonnet-5 (Bearer Token, us-west-2)  │
│  ├── claude-sonnet-4-6 (IAM, 3 Region LB)      │
│  │   ├── us-west-2: us.anthropic.*              │
│  │   ├── us-east-1: us.anthropic.*              │
│  │   └── eu-west-1: eu.anthropic.*              │
│  ├── claude-haiku-4 (IAM, us-west-2)           │
│  └── nova 系列 (IAM, us-west-2)                │
└─────────────────────────────────────────────────┘
```

### 关键配置路径
- `/root/.openclaw/workspace/litellm/`
- UI: `http://35.160.26.229:9191/ui`
- Master Key: `sk-0eebc345...`

---

## 2. 路由策略总览

| 策略 | 说明 | 适用场景 |
|------|------|----------|
| `simple-shuffle` | 无状态随机/加权随机 | 默认，多 Region 均衡 |
| `least-busy` | 最少活跃请求 | 高并发场景 |
| `latency-based-routing` | 最低延迟优先 | 延迟敏感型应用 |
| `usage-based-routing` | 最低 RPM/TPM 使用率 | 配额管理 |
| `cost-based-routing` | 最低成本 | 成本优先 |

### 2.1 Simple-Shuffle（当前使用）

```yaml
router_settings:
  routing_strategy: simple-shuffle
```

- 通过 `random.choices()` 实现
- 若设置了 `rpm`/`tpm`/`weight`，则加权随机
- 仅从 **非 cooldown** 的健康部署中选择

---

## 3. Prompt Cache Affinity（提示缓存亲和性）

### 配置

```yaml
router_settings:
  optional_pre_call_checks: ["prompt_caching"]
```

### 工作机制

1. 提取消息中带 `cache_control: {"type": "ephemeral"}` 的内容块
2. 对 cacheable prefix 做 hash → 生成 cache_key
3. 存储 `cache_key → model_id` 绑定，TTL=300s
4. 后续相同 prefix 的请求路由到同一部署

### ⚠️ 已知问题

- **TTL 硬编码 5 分钟**，但 Anthropic/Bedrock 的 ephemeral cache 支持 1 小时
- 相关 Issue: [#28427](https://github.com/BerriAI/litellm/issues/28427)
- 修复 PR: [#28459](https://github.com/BerriAI/litellm/pull/28459)
- **Workaround**: 修改 `litellm/router_utils/prompt_caching_cache.py` 中 `ttl=300` → `ttl=3600`

### 与其他策略的组合

评估顺序：**Tag Filter → Prompt Cache Affinity → Routing Strategy**

- 有 cache 绑定 → 直接路由，跳过 latency routing
- 无绑定 → 走正常路由策略选择，选择后记录绑定

### 核心洞察

> `simple-shuffle` 会破坏 prompt caching，因为缓存是 per-deployment 的。如果需要利用 prompt cache，应启用 affinity。

---

## 4. Tag-Based Routing（标签路由）

### 配置

```yaml
router_settings:
  enable_tag_filtering: true

model_list:
  - model_name: gpt-4
    litellm_params:
      model: openai/gpt-4
      tags: ["paid"]
  - model_name: gpt-4
    litellm_params:
      model: openai/gpt-4-free-tier
      tags: ["free"]
```

### 请求示例

```json
{
  "model": "gpt-4",
  "messages": [...],
  "tags": ["paid"]
}
```

或通过 Header: `x-litellm-tags: paid`

### 高级用法

- **Team-based tag routing**（Enterprise）：团队绑定标签 → 自动供应商亲和
- **Regex tag routing**（`tag_regex`）：匹配请求 Header（如 User-Agent）动态分配标签

### 组合逻辑

Tag filtering **先筛选**部署池，然后 routing strategy 在过滤后的子集中选择。

---

## 5. Latency-Based Routing（延迟路由）

### 配置

```yaml
router_settings:
  routing_strategy: latency-based-routing
  routing_strategy_args:
    ttl: 3600                    # 数据保留时间
    lowest_latency_buffer: 0     # 0=严格最低; 0.1=允许10%偏差
    max_latency_list_size: 10    # 滑动窗口大小
```

### 延迟计算

- **Streaming 请求**: TTFT (time to first token) / completion_tokens
- **Non-streaming**: total_response_time / completion_tokens

### 决策逻辑

1. 计算每个 deployment 的平均延迟（最近 10 次请求的滑动窗口）
2. 过滤超出 TPM/RPM 限制的 deployment
3. 按延迟排序
4. 在 `lowest_latency_buffer` 范围内随机选择

### 特殊情况

- **新部署**: latency = `[0]`（被优先选择，鼓励探索）
- **超时错误**: 惩罚 latency = `1000.0`

---

## 6. Cooldown 熔断机制

### 触发条件

| 条件 | 行为 |
|------|------|
| `num_fails > allowed_fails`（1分钟窗口内） | 触发 cooldown |
| 429 Rate Limit | **立即** cooldown（不计数） |

### 配置

```yaml
router_settings:
  allowed_fails: 2        # 允许的失败次数
  cooldown_time: 60       # 冷却时间（秒）
  num_retries: 2          # 单请求重试次数
```

### Cooldown vs Retry vs Fallback 区分

```
单请求失败 → retry (num_retries=2, 共3次尝试)
         → 全部 retry 失败 → 触发 fallback 到备选模型组
         
多请求累计失败 → allowed_fails 超限 → cooldown (移出健康池 60s)
```

### 多实例注意

- Cooldown 状态默认 **本地内存**
- 多 LiteLLM 实例需要 **Redis** 共享状态

---

## 7. Fallback 配置

```yaml
router_settings:
  fallbacks:
    - claude-sonnet-5:
        - claude-sonnet-4-6
```

### 验证方法

故意设置错误的 token → 请求 sonnet-5 → 观察响应 model 字段变为 sonnet-4-6

---

## 8. Bedrock API Key（Bearer Token）认证

### 场景

同一 EC2 实例上，IAM Role 模型和 Bearer Token 模型共存。

### 配置

```yaml
model_list:
  - model_name: claude-sonnet-5
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-5
      api_key: os.environ/AWS_BEARER_TOKEN_BEDROCK
      aws_region_name: us-west-2
```

Docker 环境变量：
```yaml
environment:
  - AWS_BEARER_TOKEN_BEDROCK=ABSK...
```

### 认证逻辑

- 如果 `api_key` 字段存在 → 使用 `Authorization: Bearer <token>`，跳过 SigV4
- **不影响**同实例上其他使用 IAM Role 的模型

---

## 9. Region 模型 ID 前缀规则

| Region | 前缀 | 示例 |
|--------|------|------|
| US (us-west-2, us-east-1) | `us.` | `us.anthropic.claude-sonnet-4-6` |
| EU (eu-west-1) | `eu.` | `eu.anthropic.claude-sonnet-4-6` |
| 无前缀 | - | 需要 inference profile |

---

## 10. 最终生产配置示例

```yaml
model_list:
  # Sonnet 5 - Bearer Token
  - model_name: claude-sonnet-5
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-5
      api_key: os.environ/AWS_BEARER_TOKEN_BEDROCK
      aws_region_name: us-west-2

  # Sonnet 4-6 - IAM 三路 LB
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-6
      aws_region_name: us-west-2
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-6
      aws_region_name: us-east-1
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: bedrock/eu.anthropic.claude-sonnet-4-6
      aws_region_name: eu-west-1

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  allowed_fails: 2
  cooldown_time: 60
  fallbacks:
    - claude-sonnet-5:
        - claude-sonnet-4-6
```

---

## 参考源码

- Prompt Cache Affinity: `litellm/router_utils/prompt_caching_cache.py`
- Latency Routing: `litellm/router_strategy/lowest_latency.py`
- Router 主逻辑: `litellm/router.py`
