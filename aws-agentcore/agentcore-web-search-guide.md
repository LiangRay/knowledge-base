# Amazon Bedrock AgentCore Web Search 学习总结

> 学习日期：2026-07-14  
> 参考资料：  
> - [AWS Blog: Add web search and browsing to your agents with Amazon Bedrock AgentCore](https://aws.amazon.com/cn/blogs/china/leveraging-amazon-bedrock-agentcore-quick-en/)  
> - [Qiita: Bedrock AgentCore に Web Search が追加されたので試してみた](https://qiita.com/leomarokun/items/47adb2c1b4e1e56fa0ce)  
> - [AWS Docs: Web Search Tool](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-web-search.html)

---

## 1. 概述

**Web Search on Amazon Bedrock AgentCore** 是 AWS 于 2026年6月16日发布的全托管 Web 搜索工具。通过 AgentCore Gateway 以 MCP（Model Context Protocol）标准协议暴露，任何支持 MCP 的 Agent（Claude Code、Cline、OpenCode、Strands Agents 等）只需一个 URL + IAM 认证即可获得实时网页搜索能力。

### 核心特点

| 特性 | 说明 |
|------|------|
| 协议 | MCP (Model Context Protocol) |
| 搜索引擎 | Amazon 自有搜索引擎（非第三方） |
| 数据安全 | 查询在 AWS 内部完成，不送外部搜索引擎 |
| Region | 仅 `us-east-1`（截至 2026-07） |
| 定价 | $7 / 1,000 queries |
| 查询限制 | 自然语言，200 字符以内 |
| 结果数量 | 1~25 条，默认 10 条 |
| 认证方式 | AWS IAM |

---

## 2. 架构原理

```
Agent (Claude Code / Strands / Cline)
    ↓ MCP tools/call
AgentCore Gateway (MCP endpoint)
    ↓ 内部认证 (Service Role)
Amazon Web Search Engine + Knowledge Graph
    ↓
返回结构化搜索结果（含引用和语义摘要）
```

- Gateway 使用 Service Role 对搜索请求进行内部认证
- 搜索结果是语义摘要片段（非完整 HTML），信息密度高
- Knowledge Graph 提供高置信度事实答案（如人物职位、公司成立时间等）
- 索引持续刷新，新内容分钟级可见

---

## 3. 提供的工具

| Tool | 功能 | 典型场景 |
|------|------|---------|
| `web_search` | Web 搜索，返回结构化带引用的实时结果 | 最新新闻、市场数据、事实核查 |
| `web_fetch` | 读取指定 URL 主要内容（动态页自动渲染） | 深入阅读文章、文档 |
| `web_browser`（可选） | 完整浏览器自动化 | 需要点击、填表、登录、多 tab 操作 |

### web_search 输入参数

| 参数 | 类型 | 必须 | 说明 |
|------|------|------|------|
| query | string | Yes | 搜索查询，200字符以内 |
| maxResults | integer | No | 返回条数，1~25，默认10 |

---

## 4. 部署方式

### 方式一：Console 创建（最简单，推荐测试用）

1. 打开 [Amazon Bedrock AgentCore Console](https://us-east-1.console.aws.amazon.com/bedrock-agentcore/home?region=us-east-1#/gateways)
2. 左侧菜单 → **Gateways** → **Create gateway**
3. 填写 Gateway 名称
4. Inbound Identity → 测试可选 `No authorization`，生产用 `AWS_IAM`
5. Add targets → **MCP target** → Target type: **Connectors** → Pre-configured: **Web Search Tools**
6. Review and create → 完成

### 方式二：AWS CLI 创建

```bash
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1. 创建 Gateway 执行角色
aws iam create-role --role-name BedrockSearchGatewayRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
      "Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name BedrockSearchGatewayRole \
  --policy-name websearch --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeGateway\",
       \"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:gateway/*\"},
      {\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeWebSearch\",
       \"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:aws:tool/web-search.v1\"}]}"

# 2. 创建 MCP Gateway
GW_ID=$(aws bedrock-agentcore-control create-gateway \
  --region "$REGION" --name bedrock-search-gw \
  --role-arn "arn:aws:iam::${ACCOUNT}:role/BedrockSearchGatewayRole" \
  --protocol-type MCP --authorizer-type AWS_IAM \
  --query 'gatewayId' --output text)

# 3. 挂载 Web Search Connector
aws bedrock-agentcore-control create-gateway-target \
  --region "$REGION" --gateway-identifier "$GW_ID" --name web-search \
  --target-configuration '{"mcp":{"connector":{"source":{"connectorId":"web-search"},
    "configurations":[{"name":"WebSearch","parameterValues":{}}]}}}' \
  --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]'

# 4. 获取 MCP 端点 URL
aws bedrock-agentcore-control get-gateway --region "$REGION" \
  --gateway-identifier "$GW_ID" --query 'gatewayUrl' --output text
```

> ⚠️ **AWS CLI 版本要求**：需要 2026年6月之后的版本。`aws-cli 2.34.x` 的本地 service model 不包含 `connector` target type，会报 `ParamValidation` 错误。升级到 2.35+ 即可。

---

## 5. Python 调用示例（Strands Agents）

### 安装依赖

```bash
pip install strands-agents mcp-proxy-for-aws
```

### 代码

```python
from datetime import date
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

# Gateway MCP 端点
gateway_url = "https://gateway-<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

# MCP 客户端 - 通过 IAM 认证连接 Gateway
mcp_client = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=gateway_url,
    aws_region="us-east-1",
    aws_service="bedrock-agentcore",
))

# Bedrock 模型
model = BedrockModel(
    model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    region_name="us-east-1",
    max_tokens=4096,
)

system_prompt = (
    f"你是一个有用的助手。今天是 {date.today().isoformat()}。"
    "必要时请使用可用工具搜索最新信息。"
)

with mcp_client:
    tools = mcp_client.list_tools_sync()  # 自动发现 WebSearch 工具
    print(f"发现工具: {[t.tool_name for t in tools]}")
    
    agent = Agent(model=model, tools=tools, system_prompt=system_prompt)
    result = agent("搜索2026年7月AWS最新发布的服务")
    print(result)
```

### 实测结果

- 工具名称：`web-search___WebSearch`
- Agent 自动决策是否调用搜索
- 返回结构化结果，包含标题、摘要、URL 引用

---

## 6. 与 Claude Code / Cline 集成

### Claude Code

```bash
claude mcp add --transport http web \
  https://gateway-<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

### Cline / 其他 MCP 客户端

在 `mcp.json` 中配置：

```json
{
  "mcpServers": {
    "web": {
      "type": "http",
      "url": "https://gateway-<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

> 注：如果 Gateway 使用 AWS_IAM 认证（推荐），需通过 `mcp-proxy-for-aws` 做 SigV4 签名，或部署中间层（如博客中的 EKS Helm chart）来转换为 Bearer Token 认证。

---

## 7. 与 LiteLLM Web Search 的对比

| 维度 | LiteLLM web_search_options | AgentCore Web Search |
|------|---------------------------|---------------------|
| 本质 | 模型 Provider 自带的搜索能力 | 独立 MCP 服务 |
| 支持的模型 | OpenAI search models / xAI / Anthropic / Gemini | 任何 MCP 兼容 Agent |
| 搜索引擎 | 各 Provider 自有 | Amazon 自有搜索引擎 |
| 数据安全 | 依赖 Provider | 查询不出 AWS |
| 集成方式 | API 参数 (`web_search_options`) | MCP 协议端点 |
| 适用场景 | LLM 请求中附带搜索 | Agent 工具调用 |
| 费用 | 按 Provider 定价 | $7/千次查询 |

---

## 8. 注意事项

1. **Region 限制**：Web Search Tool 目前仅在 `us-east-1` 可用
2. **引用要求**：向终端用户展示搜索结果时，必须保留出处引用和链接
3. **禁止行为**：不允许批量抽取/存储搜索结果来构建竞争性索引或数据库
4. **AWS CLI 版本**：必须使用 2026年6月之后的版本（≥2.35.x）
5. **模型兼容性**：使用 Bedrock 模型时需用 inference profile ID（`us.` 前缀），不能用裸模型 ID
6. **查询长度**：单次查询 200 字符上限
7. **Browser Tool**：如需网页浏览自动化，需在 Gateway 同时启用 Browser connector

---

## 9. 我的实测环境

| 资源 | 值 |
|------|---|
| Gateway ID | `bedrock-search-gw-v2tagaojws` |
| MCP 端点 | `https://bedrock-search-gw-v2tagaojws.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp` |
| 认证方式 | AWS IAM |
| 测试模型 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| 测试结果 | ✅ 成功调用 Web Search，返回实时搜索结果 |

---

## 10. 总结

AgentCore Web Search 解决了 Agent 接入实时搜索的"最后一公里"问题：

- **无需管理搜索 API key**（不再需要 Tavily / SerpAPI 等第三方）
- **数据不出 AWS**（合规友好）
- **标准 MCP 协议**（一次部署，所有 MCP Agent 可用）
- **Serverless 弹性**（按用量付费，无需预置）
- **Amazon 自有搜索引擎 + Knowledge Graph**（覆盖广、更新快）

对于企业级 Agent 应用，这是目前在 AWS 生态内最简洁的实时搜索集成方案。
