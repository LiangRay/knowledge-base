# 跨区 VPCE：在欧洲（法兰克福）私网访问美西 us-west-2 Bedrock — PoC 实测记录

> 测试日期：2026-11-28 ｜ 结论：**通过**

## 一句话结论

在 eu-central-1 建一个指向 `com.amazonaws.us-west-2.bedrock-runtime` 的**跨区 Interface VPC Endpoint**，法兰克福 VPC（以及经 DX 接入的 IDC）就能纯私网调用 us-west-2 的 Bedrock。Claude（Converse / ConverseStream）和 GPT（OpenAI 兼容层的 Responses / Chat Completions，含流式）都能用，**专线和路由都不用改**。

## 场景

| 项 | 值 |
|---|---|
| VPCE 所在账户 | 账户 A（集中建，B/C 共用） |
| VPCE 所在区域 | eu-central-1（法兰克福，IDC 专线落在这里） |
| 目标 Bedrock 区域 | **us-west-2** |
| 调用方 | 账户 B / C，用各自凭证 |

```
IDC ──DX──► 法兰克福 VPC（账户 A）
               └─ Interface VPCE: com.amazonaws.us-west-2.bedrock-runtime
                     │  AWS 骨干网跨区
                     ▼
               us-west-2 Bedrock Runtime（按调用方 B/C 计费、算配额、记 CloudTrail）
```

---

## 测试步骤

### 0. 前置检查：确认法兰克福支持跨区 bedrock-runtime

```bash
aws ec2 describe-vpc-endpoint-services --region eu-central-1 \
  --service-regions us-west-2 \
  --query "ServiceDetails[?contains(ServiceName,'bedrock')].[ServiceName,ServiceRegion]" --output text
```

结果：可以建 `com.amazonaws.us-west-2.bedrock-runtime`（us-east-1、us-east-2 也支持）。
**`bedrock-mantle` 不在跨区支持列表里**，所以跨区方案只能走 runtime。

### 1. 创建测试资源（eu-central-1，全部打 tag `purpose=temp-xregion-vpce-poc`）

```bash
export AWS_REGION=eu-central-1
VPC_ID=<vpc-id>; SUBNETS="<subnet-a> <subnet-b>"; VPC_CIDR=<cidr>

# 1) VPCE 用的安全组：只放行 VPC 内的 443
SG=$(aws ec2 create-security-group --group-name temp-xregion-vpce-poc \
  --description "temp PoC cross-region bedrock vpce" --vpc-id $VPC_ID --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 443 --cidr $VPC_CIDR

# 2) 跨区 Interface Endpoint（关键参数：--service-region us-west-2）
aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id $VPC_ID \
  --service-name com.amazonaws.us-west-2.bedrock-runtime \
  --service-region us-west-2 \
  --subnet-ids $SUBNETS --security-group-ids $SG \
  --no-private-dns-enabled \
  --tag-specifications 'ResourceType=vpc-endpoint,Tags=[{Key=purpose,Value=temp-xregion-vpce-poc}]'

# 3) 测试机：同 VPC 的一台小 EC2（带 SSM 角色，通过 SSM 下发命令，不开 SSH）
```

- VPCE 从 `pending` 变成 `available` 大约要**几分钟**。
- 跨区 VPCE 用**endpoint 专属 DNS**访问：`https://vpce-xxxx-yyyy.bedrock-runtime.us-west-2.vpce.amazonaws.com`，SDK/CLI 里通过 `--endpoint-url` 指定。

### 2. 连通性 + DNS

在测试机上执行 `getent hosts <vpce-dns>`，解析结果是 **VPC 内两个 AZ 的私网 IP**（172.31.x.x）。

### 3. 功能测试（关键：SigV4 必须签 us-west-2）

```bash
H=<vpce-dns>
eval $(aws configure export-credentials --format env)

# (a) Claude — Converse
aws bedrock-runtime converse --region us-west-2 --endpoint-url https://$H \
  --model-id us.anthropic.claude-sonnet-4-6 \
  --messages '[{"role":"user","content":[{"text":"pong?"}]}]' --inference-config maxTokens=10

# (b) Claude — ConverseStream（用 curl 直接测，避免依赖 CLI/SDK 版本）
curl -sN --aws-sigv4 "aws:amz:us-west-2:bedrock" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":[{"text":"Count 1 to 5"}]}],"inferenceConfig":{"maxTokens":50}}' \
  https://$H/model/us.anthropic.claude-sonnet-4-6/converse-stream

# (c) GPT — Responses API（OpenAI 兼容层，路径前缀 /openai/v1）
curl -s --aws-sigv4 "aws:amz:us-west-2:bedrock" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"global.openai.gpt-5.6-luna","input":"Reply with exactly: pong","max_output_tokens":32}' \
  https://$H/openai/v1/responses

# (d) GPT — Responses 流式：请求体加 "stream":true
# (e) GPT — Chat Completions：https://$H/openai/v1/chat/completions
```

### 4. 验证流量确实经过 VPCE（CloudTrail，us-west-2）

```bash
aws cloudtrail lookup-events --region us-west-2 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=Converse --max-results 20
```

事件里有这些字段：`awsRegion=us-west-2`、`sourceIPAddress=<法兰克福测试机私网IP>`、`vpcEndpointId=<VPCE ID>`、`vpcEndpointAccountId=<账户A>`。
**说明调用确实经过法兰克福这个 VPCE 落到 us-west-2，审计记录也带上了 VPCE 信息。**

### 5. VPCE 策略按账户放行/拦截

```bash
# 只放行指定账户（生产中填 B、C 的账户 ID）
aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> --policy-document '{
 "Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"bedrock:*","Resource":"*",
 "Condition":{"StringEquals":{"aws:PrincipalAccount":["<B账户>","<C账户>"]}}}]}'
```

| 策略 | 调用结果 |
|---|---|
| 放行本账户 | 200 |
| 只放行一个无关账户（111122223333） | `AccessDeniedException ... because no VPC endpoint policy allows the bedrock:InvokeModel action` |

⚠️ **策略生效有延迟**：实测改完后 **1～3 分钟**内结果会来回跳（有时成功有时拒绝），等约 3 分钟才稳定。做演示或变更时要预留这段时间，不要改完马上判断结果。

### 6. 清理

```bash
aws ec2 terminate-instances --instance-ids <test-instance>
aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <vpce-id>
aws ec2 wait instance-terminated --instance-ids <test-instance>
aws ec2 delete-security-group --group-id <sg-id>
# 按 tag 复查有没有遗留
aws ec2 describe-vpc-endpoints --filters Name=tag:purpose,Values=temp-xregion-vpce-poc
```

---

## 测试结果汇总

| 用例 | 结果 | 数据 |
|---|---|---|
| DNS 解析 | ✅ | 解析到 2 个 AZ 的 VPC 私网 IP |
| Claude Converse | ✅ | 5 次全部成功，端到端约 1.6s（CLI，含进程启动） |
| Claude ConverseStream | ✅ | HTTP 200，收到 5 个 contentBlockDelta，TTFB 约 1.07s |
| GPT Responses（非流式） | ✅ | 8/8 次 200，两个 AZ 的 ENI 都测过，总耗时约 0.79～0.84s |
| GPT Responses（流式） | ✅ | 事件完整：created → 13 个 output_text.delta → completed |
| GPT Chat Completions | ✅ | 10/10 次 200，总耗时约 0.85～1.03s |
| CloudTrail 记录 VPCE | ✅ | 带 vpcEndpointId / vpcEndpointAccountId |
| VPCE 策略按账户拦截 | ✅ | 非白名单账户被拒（需等策略传播） |

**网络开销**（curl 计时，测试机 → 跨区 VPCE → us-west-2）：
- TCP connect 约 **150ms**，TLS 完成约 **300ms**，基本就是法兰克福到俄勒冈的 RTT（约 150ms）。
- 每个**新连接**多付约 2 个 RTT（约 300ms）。用连接复用（keep-alive，SDK/LiteLLM 默认开启）后，每次请求只多约 1 个 RTT。
- 对比：如果目标是 us-east-1，RTT 约 90ms，延迟会更小。

## 落地要点（讲给客户听的版本）

1. **账单、配额、审计都归调用方**：B/C 用自己的凭证签名，模型费用、us-west-2 配额、CloudTrail 都记在 B/C。A 只承担 VPCE 小时费、数据处理费，以及跨区数据传输费。
2. **SigV4 region 必须是 us-west-2**：签成 eu-central-1 会报签名不匹配。endpoint-url 用 VPCE DNS，region 填目标区域。
3. **只支持 runtime，不支持 mantle**：GPT 要走 runtime 的 `/openai/v1`（模型名带 `global.` 前缀）。如果某个模型或功能只在 mantle 上有，就要改走"DXGW 直连 us-west-2 VPC"的方案。
4. **强制 B/C 必须走 VPCE**：B/C 的 IAM 加一条 Deny，条件是 `aws:SourceVpce` 不等于 A 的 VPCE ID；A 的 VPCE 策略用 `aws:PrincipalAccount` 只放行 B/C。
5. **IDC 侧 DNS**：跨区 VPCE 用的是专属 DNS 名，IDC 要能解析它。可以用 Route 53 Resolver 入站端点做转发，或者直接在 IDC 侧配 hosts/私有区。
6. **LiteLLM 在 B/C 间做负载均衡时**：GPT 要开 `encrypted_content_affinity`，因为 reasoning 加密内容绑定账户，跨账户切换会报 `invalid_encrypted_content`。

## 踩坑

- 测试机上老版本 AWS CLI 没有 `converse-stream` 子命令，系统 Python 也没有 boto3/pip。改用 `curl --aws-sigv4` 直接打 REST 接口最省事（`/model/{id}/converse-stream`），二进制 event-stream 用 grep 数事件个数即可。
- VPCE 策略修改是最终一致的，几分钟内结果会来回跳，见第 5 步。
