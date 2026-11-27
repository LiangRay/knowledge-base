# 多账户共用一套 Bedrock VPCE（集中在账户 A，B/C 共用）

> 讲解版。场景：客户的 IDC 经 Direct Connect 接到**账户 A**，Bedrock 的用量和账单在**账户 B、C**。目标是 IDC 上的 LiteLLM 走内网调用 B/C 的 Bedrock，并且尽量少建网络资源。
>
> 状态：方案根据 AWS 的鉴权和计费机制推导，单账户链路（LiteLLM → VPCE → Bedrock）已实测。**跨账户部分还没有实测**，落地前请按第 8 节逐项验证。

## 一句话结论

**VPCE 只是一条网络通道，不决定请求属于哪个账户。请求属于哪个账户，由签名用的凭证决定。** 所以只要在 A 建一套 Bedrock VPCE，IDC 用 B 的凭证调用就进入 B 的 Bedrock，用 C 的凭证就进入 C 的。B 和 C 不需要建 VPC、VGW 或 VPCE。

---

## 1. 为什么 A 的 VPCE 能访问 B 的 Bedrock

一个常见的误解是"VPCE 属于 A，所以只能访问 A 的 Bedrock"。实际上：

| 层次 | 谁决定 | 说明 |
|---|---|---|
| 网络层：流量从哪里进入 AWS 服务 | **VPCE**（A 的） | VPCE 把 VPC 内的私网 IP 接到 Bedrock 在该区域的**公共服务前端**。这个前端所有账户共用，不按账户区分 |
| 鉴权层：这个请求是谁发的 | **SigV4 签名 / API Key**（B 的） | Bedrock 收到请求后验证签名，确定调用者是 B 的某个身份 |
| 计费和配额：记在谁的账上 | **调用者所在的账户**（B） | token 费、RPM/TPM 配额、CloudTrail 记录都归 B |

> 类比：A 的 VPCE 像大楼的员工专用通道，B 和 C 是楼里的租户。通道是物业（A）修的，但进门刷的是各自的工牌（凭证），记账也记在各自名下。

同样的道理也适用于其他服务。比如在 A 的 VPC 里用 S3 Gateway Endpoint，可以访问 B 账户的桶，前提是 B 的桶策略允许。VPCE 本身没有"只能访问本账户资源"的限制，跨账户能不能用，取决于两边的**策略**是否放行（见第 3 节）。

**每个请求要通过两道检查：**
1. **A 的 VPCE 策略**：这个调用者是否允许走这条通道？
2. **B 的 IAM 策略**：这个身份是否有权限调用这个模型？

两道检查都允许，请求才会成功。

---

## 2. 架构

```
IDC (LiteLLM) ──DX──► Private VIF ──► VGW / DXGW（A）──► VPC-A
                                                       ├ bedrock-runtime VPCE
                                                       └ bedrock-mantle  VPCE
                                                              │
                          用 B 的凭证签名 ─────────────────► Bedrock（B 计费、B 的配额）
                          用 C 的凭证签名 ─────────────────► Bedrock（C 计费、C 的配额）
```

**和每个账户各建一套 VPCE 相比，省掉了这些：**
- B 和 C 不用建 VPC 和 VGW，也不需要跨账户关联 DXGW。
- 网段只需要协调 A 和 IDC 两方。
- IDC 的 BGP 只需要学到 A 的一个网段。
- 只有一套 VPCE 和安全组，费用也只付一份。

---

## 3. 关键配置：A 和 B/C 两边都要限制

### ① A：VPCE 策略只允许 B 和 C 的身份

```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "bedrock:*",
    "Resource": "*",
    "Condition": {
      "StringEquals": { "aws:PrincipalAccount": ["<B_ACCOUNT_ID>", "<C_ACCOUNT_ID>"] }
    }
  }]
}
```

- 如果 B 和 C 在同一个 AWS Organization 里，可以改用 `aws:PrincipalOrgID`，以后加账户不用改这条策略。
- **不要用 `aws:ResourceAccount` 做限制。** 基础模型的 ARN 里没有账户 ID（`arn:aws:bedrock:<region>::foundation-model/...`），用它限制会把正常请求拦掉。

### ② B/C：调用身份只能经过 A 的 VPCE 使用

```json
{
  "Statement": [
    { "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
      "Resource": "*" },
    { "Effect": "Deny",
      "Action": "bedrock:*",
      "Resource": "*",
      "Condition": {
        "StringNotEquals": { "aws:SourceVpce": ["<vpce-A-runtime>", "<vpce-A-mantle>"] }
      } }
  ]
}
```

- 作用：凭证即使泄露，也不能从公网调用。
- 如果用 **Bedrock API Key**：这把 Key 在 B 账户里对应一个 IAM 用户（`BedrockAPIKey-xxxx`），把这条策略挂到这个用户上。
- 这个身份要**专门给 LiteLLM 用**。挂了 Deny 之后，它在控制台或 Playground 里也调不了 Bedrock。

### ③ A：VPCE 安全组放行 IDC 网段访问 443

只放行 LiteLLM 所在网段（或具体主机 /32）的 TCP 443，不要整个 IDC 大段都放。

---

## 3.5 进一步收紧：只允许 IDC 上 LiteLLM 的 IP 访问

**一句话结论：** 专线（DX）全程不做 NAT，VPCE 看到的就是 IDC 主机的**私网源 IP**，所以可以按 IP 做三层限制：安全组 → VPCE 策略 → B/C 的 IAM Deny。只能做一层时，优先做安全组。

### 为什么专线下源 IP 不变

```
IDC LiteLLM (10.10.5.20) ──DX / Private VIF──> VGW/DXGW ──> A 的 VPC ──> VPCE ENI
                         这一路只做路由，不做地址转换
```

- DX、Private VIF、VGW、DXGW 都只转发，不改源地址。VPCE 收到的包，源 IP 就是 `10.10.5.20`。
- 类比：专线像一条直达的内部走廊，访客进门时还挂着自己的工牌。走公网 + NAT 则像换了一张访客证，门卫只能看到访客证。
- 前提：**IDC 内部没有 SNAT**。如果 LiteLLM 跑在 K8s 上，或者出口经过防火墙/负载均衡做了 SNAT，要写的是 SNAT 之后的 IP（K8s 一般是节点网段或出口网关 IP）。

### 三层限制

| 层 | 在哪配 | 写法 | 拦住什么 |
|---|---|---|---|
| ① 安全组 | A 的 VPCE 安全组 | 入站 443 只放 `<LITELLM_IP>/32` | 网络层直接拒绝，其他主机连 TCP 都建不起来 |
| ② VPCE 策略 | A 的 VPCE | `aws:VpcSourceIp` | 能连上但不是 LiteLLM 的请求（例如安全组被误放宽） |
| ③ IAM Deny | B/C 的 LiteLLM 专用身份 | `aws:SourceVpce` + `aws:VpcSourceIp` 两条 Deny | 凭证泄露后，从别处或别的 VPCE 调用 |

**② A 的 VPCE 策略**（和 `aws:PrincipalAccount` 写在同一个 Condition 里，两者是"并且"关系）：

```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "bedrock:*",
    "Resource": "*",
    "Condition": {
      "StringEquals": { "aws:PrincipalAccount": ["<B_ACCOUNT_ID>", "<C_ACCOUNT_ID>"] },
      "IpAddress":    { "aws:VpcSourceIp": ["<LITELLM_IP>/32"] }
    }
  }]
}
```

**③ B/C 的 IAM 策略**（在第 3 节②的基础上，再加一条 Deny）：

```json
{ "Effect": "Deny", "Action": "bedrock:*", "Resource": "*",
  "Condition": { "StringNotEquals": { "aws:SourceVpce": ["<vpce-A-runtime>", "<vpce-A-mantle>"] } } },
{ "Effect": "Deny", "Action": "bedrock:*", "Resource": "*",
  "Condition": { "NotIpAddress": { "aws:VpcSourceIp": ["<LITELLM_IP>/32"] } } }
```

- **两条 Deny 必须分开写。** 同一个 Condition 里的多个条件是"并且"，合成一条就变成"既不走这个 VPCE、又不是这个 IP 才拒绝"，会留出漏洞。
- 用 `aws:VpcSourceIp`，**不要用 `aws:SourceIp`**。请求经过 VPCE 时请求上下文里没有 `aws:SourceIp`。Allow + `IpAddress` 匹配不上，请求被拒；Deny + `NotIpAddress` 遇到缺失的键会判定为"真"，同样全部拒绝。两种写法都会把正常请求拦掉。

### 怎么确认 VPCE 实际看到的源 IP

不要靠猜，看 A 的 VPC Flow Logs：

1. 在 VPCE 的 ENI（或整个子网）上开 Flow Logs。
2. 在 LiteLLM 上发一次请求。
3. 找目标地址是 VPCE ENI IP、目标端口 443 的记录，`srcaddr` 就是要写进策略的 IP。

```
# CloudWatch Logs Insights
fields @timestamp, srcaddr, dstaddr, dstport, action
| filter dstport = 443 and dstaddr like /^<VPCE_ENI_IP_PREFIX>/
| stats count() by srcaddr
```

如果 `srcaddr` 不是 LiteLLM 主机的 IP，说明 IDC 内部做了 SNAT，以 `srcaddr` 为准。

### 注意事项

- **网段不能重叠**：IDC 网段和 A 的 VPC 网段重叠时路由会出问题，IP 限制也会失去意义。
- **区域要对**：VPCE 要建在 DX 落地的 EU 区域（如 eu-central-1），模型用 `eu.` 推理配置文件。
- **换机器要改三处**：LiteLLM 迁移、扩容或改 IP 时，安全组、VPCE 策略、B/C 的 IAM 策略都要同步改。建议写成 CIDR 变量，用 IaC 统一管理。
- **多台 LiteLLM 时**：写一个专用的小网段（如 `/28`），不要逐个写 /32。
- **待实测**：Bedrock 对 `aws:VpcSourceIp` 的支持情况还没有实测，上线前按第 8 节的第 6、7 项验证。

---

## 4. LiteLLM 配置

B 和 C 的模型用**同一个 VPCE 域名**，只有凭证不同：

```yaml
model_list:
  - model_name: claude-sonnet            # 账户 B
    litellm_params:
      model: bedrock/eu.anthropic.claude-sonnet-4-6
      aws_region_name: eu-central-1
      aws_bedrock_runtime_endpoint: https://vpce-<A-id>-<hash>.bedrock-runtime.eu-central-1.vpce.amazonaws.com
      aws_access_key_id: os.environ/B_AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/B_AWS_SECRET_ACCESS_KEY
  - model_name: claude-sonnet            # 账户 C（model_name 相同，用来负载均衡）
    litellm_params:
      model: bedrock/eu.anthropic.claude-sonnet-4-6
      aws_region_name: eu-central-1
      aws_bedrock_runtime_endpoint: https://vpce-<A-id>-<hash>.bedrock-runtime.eu-central-1.vpce.amazonaws.com
      aws_access_key_id: os.environ/C_AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/C_AWS_SECRET_ACCESS_KEY

router_settings:
  routing_strategy: simple-shuffle   # 也可以按需改成 usage-based-routing-v2
  allowed_fails: 3
  cooldown_time: 30
```

**附带的好处：配额可以叠加。** Bedrock 的 RPM/TPM 配额按账户计算。两个部署用同一个 `model_name` 时，LiteLLM 会在 B 和 C 之间负载均衡，吞吐上限接近翻倍。其中一个账户被限流（429）时，LiteLLM 会先冷却它，请求自动切到另一个。

如果要求各业务线只用自己的账户，可以给两个部署起不同的 `model_name`，或者用 Tag 路由把团队绑定到对应账户。

**凭证选择：**

| 方式 | 说明 |
|---|---|
| IAM 用户 AK/SK | 最简单。需要定期轮换 |
| Bedrock API Key | Bearer Token，适合 OpenAI 兼容层（runtime 的 `/openai/v1`、mantle 的 `/v1`） |
| IAM Roles Anywhere | 用证书换取 B/C 的临时凭证，不存长期密钥。换凭证要调用 STS，所以 A 还需要建 **STS 的 VPCE**，VPCE 策略同样只放行 B 和 C |

---

## 5. 费用归属

| 费用 | 记在哪个账户 |
|---|---|
| Bedrock token 费 | **B / C**（签名者所在账户） |
| Bedrock 配额 | **B / C** 各自的配额 |
| VPCE 小时费和数据处理费 | **A** |
| DX 端口费和出向流量费 | **A** |

A 承担的网络费用大约是 token 费的万分之一。以 us-west-2 价格估算：VPCE 每个可用区每小时 $0.01，数据处理每 GB $0.01；每 10 亿 token 约 5–10 GB，即 $0.05–0.1。EU 区域单价略有差异，量级相同。如果需要内部分摊，可以用 A 的 VPC Flow Logs 统计流量。

---

## 6. 审计

| 来源 | 能看到什么 |
|---|---|
| **B/C 的 CloudTrail** | 每次调用的事件里带 `vpcEndpointId` 和 `vpcEndpointAccountId`（A 的账户 ID），可以证明请求走的是内网 |
| A 的 VPC Flow Logs | 只能看到 IDC IP → VPCE 443 的连接，**看不到账户和模型** |
| LiteLLM 日志 | 按用户、团队、模型统计的主要依据 |

注意：InvokeModel 这类数据面事件默认不一定会记录。需要的话，在 B/C 的 CloudTrail 里打开 Bedrock 数据事件（会额外收费）。

---

## 7. 区域

VPCE 必须和要调用的 Bedrock 在同一个区域：
- **DX 在 EU，Bedrock 也在 EU**：A 在 EU 区域建 VPC 和 VPCE，最简单。
- **DX 在 EU，Bedrock 要用 us-west-2**：A 在 us-west-2 再建一个 VPC 和 VPCE，用 A 自己的 DXGW 关联这个 VPC 的 VGW。整个链路仍然只在 A 一个账户内，B 和 C 照样不用建任何网络资源。代价是跨大西洋，往返延迟大约多 140ms。

---

## 8. 落地步骤和验证

1. **A**：规划 VPC 网段，避开 IDC 的网段，也不要用 default VPC 的 172.31.0.0/16。把 Private VIF 接到 VGW 或 DXGW。
2. **A**：建 bedrock-runtime（以及需要的话 bedrock-mantle）的 VPCE，至少 2 个可用区，开启 Private DNS。安全组放行 IDC 网段访问 443，VPCE 策略按第 3 节①配置。具体命令见 [litellm-idc-via-dx.md](./litellm-idc-via-dx.md) 第 1.5 节。
3. **B / C**：确认需要的模型在本账户已开通。创建 LiteLLM 专用的调用身份，挂上第 3 节②的策略。
4. **IDC**：按第 4 节配置 LiteLLM。
5. **验证**（每项都要做）：

| # | 测试 | 期望结果 |
|---|---|---|
| 1 | 用 B 的凭证经 A 的 VPCE 调用 | ✅ 成功；B 的 CloudTrail 里 `vpcEndpointId` 是 A 的 VPCE |
| 2 | 用 C 的凭证经 A 的 VPCE 调用 | ✅ 成功，计费记在 C |
| 3 | 用 B 的凭证从公网直接调用 | ❌ AccessDenied（B 的 Deny 生效） |
| 4 | 用第三个账户 D 的凭证经 A 的 VPCE 调用 | ❌ `no VPC endpoint policy allows`（A 的 VPCE 策略生效） |
| 5 | 抓包查看 LiteLLM 出向 443 的连接 | 目标全部是 A 的 VPCE 私网 IP |
| 6 | （做了 3.5 节时）从 IDC 另一台主机用 B 的凭证经 VPCE 调用 | ❌ 连接超时（安全组拦截）；临时放开安全组后应返回 AccessDenied（IP 条件生效） |
| 7 | 查 Flow Logs 的 `srcaddr` | 等于策略里写的 LiteLLM IP |

---

## 9. 和"B/C 各自建 VPCE"怎么选

| 维度 | 集中在 A（本方案） | B/C 各自建（DXGW + 跨账户 VGW） |
|---|---|---|
| 网络复杂度 | 低，只有 1 个 VPC | 3 个 VPC，要跨账户关联 DXGW |
| 网段规划 | 只需协调 A 和 IDC | A、B、C、IDC 都不能重叠 |
| 新增账户 | 改 VPCE 策略；用 OrgID 的话不用改 | 新建 VPC、VGW 并做关联 |
| 爆炸半径 | A 的 VPCE 出问题，B 和 C 都受影响（至少用 2 个可用区） | 各自独立 |
| 网络归属 | A 控制 B/C 的入口；A 改了策略，B/C 就会断 | 各自掌控 |
| 合规 | 要确认 B/C 能接受"流量经过别的账户的 VPC" | 隔离更清晰 |
| VPCE 费用 | 1 套 | 2 套 |

**建议：**
- A、B、C 属于同一个集团，网络由一个团队统一管理：**选集中方案**。这也是 AWS 多账户环境里集中管理 Endpoint 的常见做法。
- B 和 C 是独立法人，或者审计要求流量不能离开本账户：选各自建。

---

## 10. 常见问题

**Q：B 的身份能在 A 的 VPCE 上做什么？会不会访问到 A 的资源？**
不会。VPCE 只提供网络通道。B 的身份能访问什么，仍然由 B 自己的 IAM 策略决定，A 的 Bedrock 资源不会因此对 B 开放。A 的 VPCE 策略只能进一步收紧（规定谁能走这条通道），不能扩大权限。

**Q：A 能看到 B 的 prompt 和回复吗？**
在网络层看不到。流量是 TLS 加密的，A 的 Flow Logs 里只有 IP、端口和字节数。但 IDC 上的 LiteLLM 能看到明文，要确认它的日志由谁管理。

**Q：A 删掉或修改 VPCE 会怎样？**
B 和 C 的调用会立即失败。所以这个 VPCE 要当作共享基础设施来管理：加删除保护流程，变更前通知各方，并对 VPCE 的状态设置告警。
