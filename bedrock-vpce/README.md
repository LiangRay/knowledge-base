# Bedrock VPC Endpoint 调用指南

通过 VPC Endpoint 私网调用 Amazon Bedrock 模型，流量不经公网。

## 架构概览

```
EC2 / Lambda / ECS
       │
       ▼ (VPC 内部流量)
VPC Endpoint (Interface)
  ├── bedrock-runtime  →  Claude, GPT-5.6 (Cross-Region Inference)
  └── bedrock-mantle   →  Mantle 模型 (DeepSeek, Qwen, Mistral 等)
       │
       ▼ (AWS 内网)
Amazon Bedrock Service
```

## 前置条件

1. **VPC Endpoint** — 创建 `com.amazonaws.<region>.bedrock-runtime` 类型为 Interface 的 VPC Endpoint
2. **安全组** — VPC Endpoint 的安全组需放行 TCP 443（来自调用方）
3. **Private DNS** — 建议开启（可选，不开启则用 VPCE 专用域名）
4. **Bedrock API Key** — 在 [Bedrock Console → API Keys](https://console.aws.amazon.com/bedrock/home#/api-keys) 创建

## 两种认证方式

### 方式一：Bearer Token（Bedrock API Key）— 推荐

零 SDK 依赖，任何 HTTP 客户端都能调用，完全兼容 OpenAI SDK。

```bash
export OPENAI_API_KEY="<Bedrock API Key>"
export OPENAI_BASE_URL="https://<vpce-id>.bedrock-runtime.<region>.vpce.amazonaws.com/openai/v1"

curl --location --request POST "${OPENAI_BASE_URL}/responses" \
  --header "Authorization: Bearer ${OPENAI_API_KEY}" \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "model": "global.openai.gpt-5.6-sol",
    "input": "hi"
  }'
```

### 方式二：SigV4 签名（IAM Role / IAM User）

适合已有 IAM 体系的场景，无需额外管理 API Key。

```python
import boto3, json, urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session(region_name='us-west-2')
credentials = session.get_credentials().get_frozen_credentials()

url = "https://<vpce-id>.bedrock-runtime.us-west-2.vpce.amazonaws.com/model/global.openai.gpt-5.6-sol/invoke"

body = json.dumps({
    "messages": [{"role": "user", "content": "hi"}]
})

request = AWSRequest(method='POST', url=url, data=body, headers={
    'Content-Type': 'application/json',
    'Accept': 'application/json',
})
SigV4Auth(credentials, 'bedrock', 'us-west-2').add_auth(request)

http = urllib3.PoolManager()
response = http.request('POST', url, headers=dict(request.headers), body=body)
print(response.data.decode())
```

## API 路径与格式对照

| 认证方式 | Base URL 路径 | API 格式 | Body 示例 |
|---------|-------------|---------|----------|
| Bearer Token | `/openai/v1/responses` | OpenAI Responses API | `{"model": "...", "input": "hi"}` |
| Bearer Token | `/openai/v1/chat/completions` | OpenAI Chat Completions | `{"model": "...", "messages": [...]}` |
| SigV4 | `/model/{modelId}/invoke` | Chat Completions（无 model 字段） | `{"messages": [...]}` |

## 模型名称

| 模型 | 模型 ID | 备注 |
|------|---------|------|
| Claude Sonnet 5 | `us.anthropic.claude-sonnet-5` | 跨区域推理 |
| Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | 跨区域推理 |
| GPT-5.6 Sol | `global.openai.gpt-5.6-sol` | 跨区域推理 |
| GPT-5.6 Terra | `global.openai.gpt-5.6-terra` | 跨区域推理 |
| GPT-5.6 Luna | `global.openai.gpt-5.6-luna` | 跨区域推理 |

> **注意**：OpenAI 兼容层（`/openai/v1`）使用带 `global.` 前缀的模型 ID；SigV4 invoke 路径也使用带前缀的 ID。

## VPC Endpoint Policy

VPCE Policy 是独立于 IAM Policy 的**第二层访问控制**，两者都必须 Allow 才能通过。

### 全开放（开发/测试）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "*",
      "Resource": "*"
    }
  ]
}
```

### 限定账号（推荐）

使用 `aws:PrincipalAccount` 条件键，允许该账号下所有 IAM 实体（包括 Bedrock API Key 自动创建的 IAM User）通过 VPCE 访问，无需逐个列出 Principal。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "*",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:PrincipalAccount": "<account-id>"
        }
      }
    }
  ]
}
```

> **优势**：新增 Bedrock API Key 或 IAM Role 时无需更新 VPCE Policy，只要在同一账号下即可访问。

### 限定具体 Principal（精细控制）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::<account-id>:user/BedrockAPIKey-xxxx",
          "arn:aws:iam::<account-id>:role/MyAppRole"
        ]
      },
      "Action": "*",
      "Resource": "*"
    }
  ]
}
```

> **注意**：Bearer Token（Bedrock API Key）的请求身份是系统自动创建的 IAM User `BedrockAPIKey-xxxx`，VPCE Policy 中的 Principal 需要包含这个 User。可通过 `aws iam list-users` 查看。

## 常见问题排查

### 1. `UnknownOperationException`
**原因**：API 路径不对。Bearer Token 必须用 `/openai/v1/responses` 路径，不能用 `/v1/responses`。

### 2. `no VPC endpoint policy allows the bedrock:InvokeModel action`
**排查步骤**：
1. 确认 VPCE Policy 挂在了 **bedrock-runtime** 的 VPCE 上（不是 bedrock-mantle）
2. 确认 Principal 包含 Bedrock API Key 对应的 IAM User
3. Policy 修改后等待几分钟传播

### 3. `The model 'xxx' does not exist`
**原因**：模型名称不匹配。bedrock-runtime 使用带 `global.` 或 `us.` 前缀的跨区域推理 ID。

### 4. 安全组连通性
VPC Endpoint 的安全组需允许调用方的 TCP 443 入站。如果 EC2 和 VPCE 在同一安全组，且有"同 SG 内全通"规则，则无需额外配置。

## 与 OpenAI SDK 集成

任何兼容 OpenAI SDK 的工具只需设置两个环境变量：

```bash
export OPENAI_API_KEY="<Bedrock API Key>"
export OPENAI_BASE_URL="https://<vpce-id>.bedrock-runtime.<region>.vpce.amazonaws.com/openai/v1"
```

支持的工具包括：
- Python `openai` 库
- Codex CLI
- LiteLLM Proxy
- Cursor / Continue 等 IDE 插件
