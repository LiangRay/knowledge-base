# Claude Code 接入第三方模型：Anthropic Messages API 兼容生态

> 整理时间：2026-10-14
> 背景：Claude Code 能用 DeepSeek、Kimi、GLM 等模型。研究这个问题，是为了给 LiteLLM 多供应商网关选型做参考。

**可信度标注**：✅ 已核实（看过一手文档，或实际调用过）　⚪ 业内常识，本次没重新验证　🔸 推测或待实测

---

## 1. 核心结论

- **Claude Code 本身没有加 DeepSeek 支持。** 是各模型厂商自己做了 **Anthropic Messages API 兼容接口**（`POST /v1/messages`）。
- Claude Code 用标准环境变量 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 指向厂商的接口就能用，CLI 不需要任何改动。✅
- 这已经是国产模型的标配：DeepSeek、Kimi、智谱、MiniMax、阿里百炼、火山方舟都提供了。✅

## 2. DeepSeek 接入 Claude Code（官方配置）✅

来源：<https://api-docs.deepseek.com/guides/anthropic_api>、<https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code>

```bash
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN=${DEEPSEEK_API_KEY}
export API_TIMEOUT_MS=600000
export ANTHROPIC_MODEL=deepseek-chat
export ANTHROPIC_SMALL_FAST_MODEL=deepseek-chat
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

claude
```

要点：
- 如果请求里带的是 Claude 模型名（例如 `claude-sonnet-4-6`），DeepSeek 会**自动映射到自己的模型**，Claude Code 的默认配置不用改也能跑。
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 用来关掉遥测这类非必要请求，防止这些请求打到第三方端点后报错。
- `API_TIMEOUT_MS` 建议调大，因为推理模型首 token 慢。

## 3. 各厂商 Anthropic 兼容端点 ✅

探测方法：用假 key（`x-api-key: test`，`anthropic-version: 2023-06-01`）向 `<base>/v1/messages` 发 POST。
判断规则：返回 **401/403 说明端点存在**，只是认证失败；返回 404 说明端点不存在。

| 厂商 | `ANTHROPIC_BASE_URL` | 探测结果 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/anthropic` | 401 |
| Kimi（Moonshot） | `https://api.moonshot.cn/anthropic` | 401 |
| 智谱 GLM | `https://open.bigmodel.cn/api/anthropic` | 401 |
| MiniMax | `https://api.minimaxi.com/anthropic` | 401 |
| 阿里百炼 | `https://dashscope.aliyuncs.com/apps/anthropic` | 403 |
| 火山方舟（Coding） | `https://ark.cn-beijing.volces.com/api/coding` | 401 |
| OpenRouter | `https://openrouter.ai/api` | 401 |

网关和本地工具也能对外暴露 `/v1/messages`：**LiteLLM Proxy**、9Router、Ollama ⚪

复现脚本：

```bash
for base in \
  https://api.deepseek.com/anthropic \
  https://api.moonshot.cn/anthropic \
  https://open.bigmodel.cn/api/anthropic \
  https://api.minimaxi.com/anthropic \
  https://dashscope.aliyuncs.com/apps/anthropic \
  https://ark.cn-beijing.volces.com/api/coding \
  https://openrouter.ai/api; do
  code=$(timeout 15 curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "$base/v1/messages" \
    -H 'x-api-key: test' -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d '{"model":"x","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}')
  echo "$code  $base"
done
```

## 4. 为什么大家都在做 ⚪

1. **Claude Code 已经是 Agent 编程的事实标准。** 兼容它的协议，就能直接用上它的工具链和用户习惯。
2. **抢编程用户。** 各家都推出了 Coding Plan 包月套餐，直接对标 Claude Pro/Max。
3. **模型层面也在对齐。** Kimi K2、GLM-4.5 及以后的版本、MiniMax M2 等在训练时专门针对 Claude Code 的工具调用格式和 Agent 循环做了优化。

## 5. 兼容性常见坑 ⚪🔸

"兼容"一般只保证主路径能跑通。下面这些容易出问题：

| 特性 | 常见情况 |
|---|---|
| `cache_control`（Prompt Cache） | 多数**直接忽略**，或者厂商自己做隐式缓存，计费方式和 Anthropic 不同 |
| `thinking` / `budget_tokens` | 预算常常不生效；interleaved thinking 和 `signature` 支持程度不一，多轮回传时可能报错 |
| `document` / `citations` / PDF | 经常不支持 |
| `server_tool_use`（web_search 等）、MCP connector | 基本不支持 |
| 模型名 | 有的会把 `claude-*` 自动映射成自家模型（如 DeepSeek），有的直接报错，需要显式设置 `ANTHROPIC_MODEL` |
| 工具调用质量 | 协议能跑通，但长链路 Agent 任务的稳定性和 Claude 还有差距 🔸 |
| 上下文长度 | 普遍比 Claude 小，Claude Code 自动压缩（compact）的触发时机可能不合适 🔸 |

## 6. 和 LiteLLM 网关结合

推荐架构：**Claude Code 统一指向 LiteLLM，LiteLLM 后面挂多个供应商。**

```
Claude Code ──ANTHROPIC_BASE_URL──▶ LiteLLM Proxy (/v1/messages)
                                      ├─ Bedrock Claude（主）
                                      ├─ DeepSeek（anthropic 兼容 或 deepseek/ 原生）
                                      ├─ Kimi / GLM / MiniMax
                                      └─ Bedrock 上的开源模型
```

好处：统一用 Virtual Key 鉴权、按 tag 路由（用户绑定供应商）、出错自动 fallback、集中审计和计费。

LiteLLM 里接 DeepSeek 有三种方式 ⚪🔸：

```yaml
model_list:
  # 方式 A：LiteLLM 原生 deepseek provider（走 OpenAI 格式，LiteLLM 负责转换协议）
  - model_name: deepseek-chat
    litellm_params:
      model: deepseek/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY

  # 方式 B：直接透传到 DeepSeek 的 Anthropic 兼容端点（协议损耗最小）
  - model_name: deepseek-anthropic
    litellm_params:
      model: anthropic/deepseek-chat
      api_base: https://api.deepseek.com/anthropic
      api_key: os.environ/DEEPSEEK_API_KEY

  # 方式 C：Bedrock 上的 DeepSeek（数据不出 AWS）
  - model_name: deepseek-bedrock
    litellm_params:
      model: bedrock/converse/us.deepseek.r1-v1:0
      aws_region_name: us-west-2
```

Claude Code 端：

```bash
export ANTHROPIC_BASE_URL=https://<litellm-host>
export ANTHROPIC_AUTH_TOKEN=sk-<litellm-virtual-key>
export ANTHROPIC_MODEL=deepseek-anthropic
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

## 7. 待实测 🔸

- [ ] 用 `claude -p` 跑真实任务，对比 DeepSeek、Kimi、GLM 三项：工具调用、thinking 多轮回传、长会话 compact
- [ ] LiteLLM 方式 A 和方式 B 的差异：协议转换后 tool_use 和 thinking 有没有丢字段
- [ ] Bedrock 上的 DeepSeek 经 LiteLLM 给 Claude Code 用：工具调用格式是否稳定（主要风险）
- [ ] 各厂商对 `cache_control` 的实际计费表现
