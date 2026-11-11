# AgentCore Web Search 实测（2026-11）：InvokeWebSearch API + Codex 通过 MCP 联网搜索 + mcp-proxy-for-aws 原理

> 姊妹篇：[agentcore-web-search-guide.md](agentcore-web-search-guide.md)（Gateway Connector 部署 / Strands 集成）
> 测试脚本：[scripts/web-search/](scripts/web-search/)
> 测试时间：2026-11-11 · 环境：EC2（us-west-2，实例角色带 IAM 权限）· Codex CLI 0.156.1 · Python 3.11 / boto3 1.44.0

---

## 1. 两种调用方式

AgentCore Web Search 现在有两种用法：

| | **Gateway Connector（MCP）** | **InvokeWebSearch API**（2026-11-10 GA） |
|---|---|---|
| 形态 | 在 Gateway 上挂 Web Search target，以 MCP 工具暴露 | 直接调用 `bedrock-agentcore` 的数据面 API |
| 前置资源 | 需要先建 Gateway、执行角色和 target | 不需要建资源，有 IAM 权限即可 |
| 调用方 | MCP 客户端（Codex / Claude Code / Kiro / Strands …） | 任何能调 AWS SDK 的代码 |
| 认证 | IAM SigV4（也可改成 JWT/OAuth） | IAM SigV4 |
| 参数名 | `query`、`maxResults`（默认 10） | `searchQuery`、`maxResults`（默认 **25**），另有 `filters` |
| Region | Gateway 所在 region | us-east-1/2、us-west-2、ap-southeast-2、ap-northeast-1、eu-west-1（不含 GovCloud，GovCloud 只能用 Connector） |
| 适用场景 | 让 Agent 或编码助手"自己决定什么时候搜" | 后端程序化检索、RAG 补充、批量查询 |

两种方式的共同点：
- 计费：**每千次查询 $7**
- 查询长度：**最多 200 字符**（201 字符会报 ValidationException）
- 返回条数：`maxResults` 取 1–25
- 合规：支持 PrivateLink 和 CloudTrail
- 配额：API 默认每账号每 region **10 TPS**

---

## 2. InvokeWebSearch API（boto3）

AWS CLI 2.35.22 还没有 `invoke-web-search` 子命令，所以用 boto3 调用（1.44.0 已支持）。

```python
import boto3
from datetime import datetime, timezone, timedelta

c = boto3.client("bedrock-agentcore", region_name="us-west-2")
r = c.invoke_web_search(
    webSearchId="web-search.v1",          # ⚠️ 不是 aws.web-search.v1
    searchQuery="Amazon Bedrock AgentCore Web Search API",
    maxResults=10,
    filters={
        "domainFilter": {"include": ["aws.amazon.com"]},   # 或 {"exclude": [...]}，最多 100 个域名
        "publishedDateFilter": {
            "publishedAfter": datetime.now(timezone.utc) - timedelta(days=7),
            # "publishedBefore": ...
        },
    },
)
for it in r["results"]:
    print(it.get("publishedAt"), it["title"], it["url"])   # 每条还有 text 正文摘要
```

### 实测（us-west-2）

| 场景 | 耗时 | 结果 |
|---|---|---|
| 基础查询 | 0.54s | ✅ |
| `domainFilter.include` | 0.27s | ✅ 结果只来自指定域名 |
| `domainFilter.exclude` | 0.26s | ✅ |
| 最近 7 天（`publishedAfter`） | 0.36s | ✅ |
| 连续 5 次调用 | p50 0.29s | ✅ |
| 不设 `maxResults` | — | 默认返回 25 条，`text` 平均约 1.6k 字符 |
| `webSearchId="aws.web-search.v1"` | — | ❌ `ResourceNotFoundException: the web search identifier is not recognized` |

### 所需 IAM 权限

```json
{ "Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch", "Resource": "*" }
```
（建议以官方文档为准，可把 Resource 收窄到 web-search 资源 ARN。）

---

## 3. Gateway Connector（MCP）实测（us-east-1）

| 场景 | 耗时 | 结果 |
|---|---|---|
| 中文查询 | 0.72s | ✅ 5 条 |
| 时效性查询 | 0.46s | ✅ |
| 技术文档查询 | 0.43s | ✅ |
| `maxResults=25` | — | ✅ 25 条 |
| 查询 201 字符 | — | ❌ 上限是 200 字符 |
| Strands Agent + Claude Haiku 4.5 | 9.1s | ✅ 模型主动搜索 3 次，消耗约 2 万 token，回答附引用 |

MCP 工具名：`web-search___WebSearch`，命名规则是 `<target 名>___<工具名>`。

---

## 4. 让 Codex CLI 通过 MCP 使用 Web Search

### 4.1 问题

AgentCore Gateway 的 MCP 接口要求 **IAM SigV4 签名**，而 Codex 的远程 MCP（streamable HTTP）只支持 Bearer Token 或 OAuth，**不会做 SigV4 签名**，因此无法直连。Claude Code、Kiro、Cursor、Cline 都是同样的情况。

### 4.2 方案：本地 stdio 代理 `mcp-proxy-for-aws`

```bash
python3 -m venv /root/.venvs/agentcore-ws
/root/.venvs/agentcore-ws/bin/pip install mcp-proxy-for-aws   # 实测 v1.8.0
```

`~/.codex/config.toml`：

```toml
web_search = "disabled"     # 关闭 Codex 自带的 web_search，统一走 AgentCore（自定义 provider 下也必须关）

[mcp_servers.agentcore-search]
command = "/root/.venvs/agentcore-ws/bin/mcp-proxy-for-aws"
args = [
  "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
  "--service", "bedrock-agentcore",
  "--region", "us-east-1",
]
startup_timeout_sec = 60
```

也可以不改配置文件，在命令行用 `-c` 临时注入：

```bash
codex exec --skip-git-repo-check \
  -c web_search=\"disabled\" \
  -c 'mcp_servers.agentcore-search.command="/root/.venvs/agentcore-ws/bin/mcp-proxy-for-aws"' \
  -c 'mcp_servers.agentcore-search.args=["https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp","--service","bedrock-agentcore","--region","us-east-1"]' \
  "搜索一下今天 AWS 有什么新发布" < /dev/null
```

### 4.3 验证

```
$ codex mcp list
agentcore-search  /root/.venvs/agentcore-ws/bin/mcp-proxy-for-aws  ...  enabled  Unsupported
aws-knowledge     https://knowledge-mcp.global.api.aws             ...  enabled  Unknown
```
Auth 一栏显示 `Unsupported` 是正常的，签名由代理负责，Codex 不需要处理认证。

实测结果（Codex 0.156.1，模型 gpt-6.1 经 LiteLLM）：

| 测试 | 工具调用 | 结果 |
|---|---|---|
| 指定用 agentcore-search 搜索并列 3 条 | `web-search___WebSearch` ×1 | ✅ 中文表格，含标题、时间、URL，约 13k tokens |
| "今天 AWS 有什么新发布"（不指定工具） | `aws-knowledge` ×1 + `WebSearch` ×2 | ✅ 模型自己选择了工具 |

### 4.4 Claude Code 接入（同样的方式）

```bash
claude mcp add agentcore-search -s user -- \
  /root/.venvs/agentcore-ws/bin/mcp-proxy-for-aws \
  https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
  --service bedrock-agentcore --region us-east-1
```

### 4.5 踩坑

| 现象 | 原因 / 解决 |
|---|---|
| `ERROR: Reconnecting... waiting for network` | 不是 MCP 的问题。Codex 的 `base_url` 填的是本机公网 IP，从本机自己访问不通（hairpin）。改成 `http://localhost:9191/v1` 即可 |
| `codex exec` 在脚本或后台运行时卡住 | 它在等待 stdin，命令末尾加 `< /dev/null` |
| `--full-auto` 下 MCP 调用被取消（"user cancelled"） | exec 模式需要改用 `--dangerously-bypass-approvals-and-sandbox`，或在配置里放行 MCP 工具 |
| 自定义 provider 报 `web search is not supported` | 设置 `web_search = "disabled"` |
| 费用不好估算 | 模型会自己决定搜几次（实测一次任务搜 1–3 次），每次按 $7/千次计费 |

---

## 5. mcp-proxy-for-aws 原理

AWS 官方开源的 MCP 代理（awslabs/mcp-proxy-for-aws），作用是把 MCP 请求加上 AWS 签名再转发出去：**对客户端是一个本地 stdio MCP Server，对 AWS 是一个会做 SigV4 签名的 HTTP 客户端**。

```
Codex / Claude Code / Kiro
   │  stdio（JSON-RPC：initialize / tools/list / tools/call）
   ▼
mcp-proxy-for-aws（客户端拉起的子进程，随会话启动和退出）
   │  HTTPS streamable-http，每个请求都现场做 SigV4 签名
   ▼
AgentCore Gateway /mcp ──IAM 鉴权──► Web Search Connector / Lambda / OpenAPI target
```

源码结构（v1.8.0）：

| 模块 | 作用 |
|---|---|
| `server.py` | 用 FastMCP 的 `FastMCPProxy` 起代理，`run_async(transport='stdio')` 对客户端提供服务；挂载各类 middleware |
| `sigv4_helper.py` | `SigV4HTTPXAuth(httpx.Auth)` 在每个请求发出前调用 botocore `SigV4Auth.add_auth()` 签名；凭证来自 boto3 标准凭证链（环境变量 → profile → SSO → 实例角色），会自动刷新 |
| `client.py` | `aws_iam_streamablehttp_client()`，也可以在 Python 里直接当 MCP 客户端用（见 `scripts/web-search/raw_search.py`） |
| middleware | 失败重试 `--retries`、工具超时 `--tool-timeout`、只读过滤 `--read-only`、多 profile 切换 `--profile a b c`、注入元数据 `--metadata`、附加 HTTP 头 `--header` |

为什么不改用 Bearer Token：

- **没有需要保管的长期密钥**：签名每次请求现算，几分钟后过期；凭证来自实例角色、SSO 或 profile。
- **权限和审计都走 IAM**：谁能调用由 IAM Policy 控制，并有 CloudTrail 记录；收回权限只要改角色，不用轮换 token。
- **通用**：所有 IAM 认证的 AWS MCP 端点都能这样接，例如 AgentCore Gateway、AgentCore Runtime 上部署的 MCP Server。

不想多一个本地进程的话，可以把 Gateway 的 inbound auth 改成 **Cognito / OAuth（JWT）**，客户端用 Bearer Token 直连。代价是要自己管理 token 的签发和刷新。

---

## 6. 选型建议

| 需求 | 推荐 |
|---|---|
| 编码助手、Agent 需要联网 | Gateway Connector + `mcp-proxy-for-aws`（本文第 4 节） |
| 后端服务或 RAG 程序化检索 | InvokeWebSearch API（不用建资源，延迟约 0.3s） |
| 需要按域名或时间过滤 | InvokeWebSearch API（`filters`） |
| GovCloud | 只能用 Gateway Connector |
| 想要直接拿到整理好、带引用的答案 | 关注 2026-11-04 发布的 **Web Answer**（本次未测） |

本次测试共约 20 次查询，费用约 $0.14。
