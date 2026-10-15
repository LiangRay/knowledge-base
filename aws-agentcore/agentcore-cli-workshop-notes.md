# AgentCore CLI Workshop 学习笔记

> 来源：Frank Kong 的 GenAI Workshop — AgentCore 章节（https://genai.kpingfan.com/11.agentcore/）
> 本文为个人整理的要点与命令速查，非原文转载；完整步骤、代码与截图请查阅原站。

## 概要

- **目标**：用 AgentCore CLI 把一个电商客服 Agent 从本地原型一路推进到生产（Memory → Gateway → 可观测 → 鉴权 → 评估）
- **技术栈**：AgentCore CLI（Node.js，`@aws/agentcore`）+ Strands Agents + Bedrock Claude Sonnet 4.5 + uv
- **Agent 工具**：
  - `get_return_policy()` — 按品类查退货政策
  - `get_product_info()` — 查产品信息/规格
  - Exa MCP — 网络搜索故障排查信息
- **开发接口分层**：AgentCore API → AWS SDK (boto3) → AgentCore Python/TS SDK → AgentCore CLI（体验最好）

## AgentCore 组件速览

| 组件 | 作用 |
|---|---|
| Runtime | 无服务器运行环境，自动扩缩，支持长任务，会话隔离 |
| Gateway | 托管的 MCP 兼容代理，把已有 API/Lambda 变成 Agent 工具 |
| Identity | Agent 身份认证，对接 Cognito / Entra ID / Okta 等 |
| Memory | 托管记忆（短期 + 长期） |
| Observability | OTel 自动埋点，数据进 CloudWatch GenAI Observability |
| Evaluations | 在线/按需质量评估 |
| Guardrails | 行为边界控制 |

## 分章要点

| # | 章节 | 关键点 |
|---|---|---|
| 1 | 环境准备 | Node 20+、uv、`npm i -g @aws/agentcore`；装过旧 `bedrock-agentcore-starter-toolkit` 要先卸载；CloudFormation 前置栈（Cognito、保修 Lambda、SSM 参数）；CloudWatch 中开启 **Transaction Search**（可观测性一次性前提） |
| 2 | 原型 I | `agentcore create` 脚手架，可选 Framework（Strands/LangGraph/CrewAI/ADK/OpenAI Agents SDK）、Model provider（Bedrock/Anthropic/OpenAI/Gemini）、Memory、Build type（CodeZip/Container）。结构：`app/<Agent>/`（代码，`main.py` 入口、`model/load.py` 模型配置）+ `agentcore/`（`agentcore.json` 配置 + CDK）。`agentcore/.llm-context/` 是给 AI 编码助手看的只读类型定义；`agentcore/.cli/deployed-state.json` 记录已部署资源 ARN |
| 3 | 原型 II | `agentcore dev` 本地 uvicorn:8080 + 热重载（自动建 .venv、uv sync）；`--logs` 非交互模式，可在另一终端 `agentcore dev "..." --stream` 调用（适合 CI）；`deploy` 通过 CDK 打包并建 IAM 角色/Runtime |
| 4 | Memory | `agentcore add memory`，策略 SEMANTIC（事实）+ SUMMARIZATION（对话摘要）；需在代码中接入 session manager；main.py 改为**按 session/user 创建 Agent 的工厂模式**，从 runtime context 取 session_id / user_id；Memory ID 由 Runtime 以环境变量 `MEMORY_SHAREDMEMORY_ID` 注入；Memory 是云服务，必须 deploy 后才可用 |
| 5 | Memory 原理 | 命名空间：`/users/{actorId}/facts`、`/summaries/{actorId}/{sessionId}`；actorId = user_id，服务端隔离；CLI invoke 默认 user 为 `default-user`（所以演示能跨会话"记住"）；生产中 user_id 应来自 JWT `sub` / OAuth 用户 ID / API Key 绑定账户；可用 `aws bedrock-agentcore list-memory-records` / `list-actors` 查看 |
| 6 | Gateway | 把现有 Lambda "MCPify"，无需改原代码；需提供工具 schema（Lambda 本身无元数据）；Gateway 调 Lambda 时**参数直接在 event 中**（不在 `event["body"]`），工具名在 `context.client_context.custom["bedrockAgentCoreToolName"]`，格式 `<TargetName>___<tool_name>`；目标类型含 Lambda、API Gateway REST stage、OpenAPI、Smithy、MCP server 等；Lambda ARN 从 SSM Parameter Store 取 |
| 7 | 可观测性 | `--session-id` 维持会话上下文，不同会话隔离，跨会话仅共享长期记忆；**session ID ≥ 33 字符**（用 UUID）；自动 OTel → CloudWatch（GenAI Observability → Bedrock AgentCore → Agents/Sessions/Traces）；首次调用后约 **10 分钟** trace 才出现 |
| 8 | Cognito 鉴权 | 在 `agentcore.json` 的 runtime 条目中加 `authorizerType`、`authorizerConfiguration`（`discoveryUrl` = Cognito OIDC 发现端点、`allowedClients` = App Client ID）和 `requestHeaderAllowlist`（让 Authorization 头透传给 MCP 客户端以访问 Gateway）；`agentcore validate` 先校验；Runtime 与 Gateway 是**独立端点，需分别保护**；**Gateway 授权配置不能原地更新** → 删除 → deploy → 带 JWT 授权重建 → 重新加 target |
| 9 | 评估 | `agentcore add online-eval`，内置评估器 `Builtin.GoalSuccessRate` / `Builtin.Correctness` / `Builtin.ToolSelectionAccuracy`；演示用 `--sampling-rate 100`，生产建议 10–20%；`--enable-on-create` 部署即生效，若为 DISABLED 用 `resume` 启用；`agentcore run eval` 对历史 trace 做按需评估；结果需几分钟处理 |
| 10 | 总结/清理 | `agentcore remove all` + `agentcore deploy` 删除 AgentCore 资源；再删 CloudFormation 前置栈 |

### 评估分数解读（参考）

| 分数 | 含义 | 动作 |
|---|---|---|
| 80–100% | 优秀 | 持续监控 |
| 60–80% | 可改进 | 复查低分会话 |
| < 60% | 需关注 | 排查根因 |

常见改进方向：GoalSuccess 低 → 优化系统提示/工具描述；Correctness 低 → 修正数据与工具返回格式；ToolSelection 低 → 改进工具描述、在提示中给示例。

## 命令速查

```bash
# 环境
npm install -g @aws/agentcore
agentcore --version
uv tool uninstall bedrock-agentcore-starter-toolkit   # 如之前装过

# 生命周期
agentcore create [--defaults]          # 不带参数为交互式向导
agentcore dev [--logs]                 # 本地开发服务器
agentcore dev "What can you do?" --stream
agentcore validate
agentcore deploy -y -v
agentcore status
agentcore invoke "..." --stream [--session-id <uuid>] [--bearer-token <jwt>]

# Memory
agentcore add memory ...
aws bedrock-agentcore list-memory-records ...
aws bedrock-agentcore list-actors ...

# Gateway
agentcore add gateway --name my-gateway --runtimes CustomerSupport
agentcore add gateway-target ...
agentcore remove gateway --name my-gateway -y

# 可观测
agentcore traces list --limit 10
agentcore traces get <trace-id> --output trace.json
agentcore logs
agentcore logs --since 1h --level error
agentcore logs --since 1h --query "warranty"

# 评估
agentcore add online-eval ...
agentcore pause  online-eval QualityMonitor
agentcore resume online-eval QualityMonitor
agentcore run eval ...
agentcore evals history --runtime CustomerSupport --limit 5

# 清理
agentcore remove all && agentcore deploy
aws cloudformation delete-stack --stack-name agentcore-workshop-prereqs
```

## 前置资源

- CloudFormation 模板（作者托管）：`https://pingfan.s3.amazonaws.com/files/prereqs-agentcore.yaml`，栈名 `agentcore-workshop-prereqs`
  - 创建 Cognito 用户池、`workshop-warranty-check` Lambda、IAM 角色、SSM 参数（Cognito discovery URL / client ID、Lambda ARN 等）
- 示例代码：AgentCore CLI 官方 GitHub 仓库

## 坑点汇总

1. 可观测性要先在 CloudWatch 开 Transaction Search，否则 trace 不进索引
2. session ID 少于 33 字符会被拒
3. CLI 默认 user 是 `default-user`，多用户测试时记得区分 user_id，否则记忆会串
4. Gateway 的 authorizer 不能原地改，需删除重建
5. Runtime 开启 JWT 后，Gateway 也要单独保护，并通过 `requestHeaderAllowlist` 透传 token
6. Gateway → Lambda 的入参在 event 顶层，不在 body
7. trace 与评估结果都有分钟级延迟（trace ~10 分钟）
8. 在线评估按采样收费，生产不要用 100%，不用时 pause
