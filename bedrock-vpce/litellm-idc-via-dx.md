# IDC 上的 LiteLLM 经专线 + VPC Endpoint 内网调用 Bedrock

> 讲解版。适用场景：客户的 LiteLLM 部署在 IDC，通过 Direct Connect（专线）连到 AWS VPC，希望调用 Bedrock 不走公网。

## 一句话结论

**在 LiteLLM 里直接把 endpoint 写成 VPCE 专属域名（`https://vpce-xxx.bedrock-runtime.<region>.vpce.amazonaws.com`）即可。IDC 侧不用改 DNS，也不用写死 172.x 的 IP。**

---

## 1. 原理：为什么不用改 DNS

每个 Interface VPC Endpoint 有两类域名：

| 域名 | 例子 | 在哪能解析到内网 IP |
|---|---|---|
| **VPCE 专属域名** | `vpce-<id>-<hash>.bedrock-runtime.us-west-2.vpce.amazonaws.com` | **任何地方**，公网 DNS 返回的也是 VPCE ENI 的私网 IP |
| 标准域名（Private DNS） | `bedrock-runtime.us-west-2.amazonaws.com` | **只在开了 Private DNS 的 VPC 内部**；在 IDC 或公网解析得到的是公网 IP |

实测（用公网 DNS 8.8.8.8 查询）：

```
vpce-...bedrock-runtime.us-west-2.vpce.amazonaws.com  →  172.31.x.x（4 个私网 IP，每个子网/AZ 一个）
bedrock-runtime.us-west-2.amazonaws.com               →  34.x / 44.x（公网 IP）
```

所以 IDC 机器只要能解析公网 DNS，拿到的就是 VPCE 私网 IP，流量自然经专线进 VPC。

> 类比：标准域名像"公司前台总机"，只有在公司里拨才会转到内部分机；VPCE 专属域名本身就是"内部分机号"，在哪儿查都是这个号，只要你有线路（专线）能拨进来。

---

## 2. LiteLLM 配置（v1.99.0 实测通过）

Bedrock 有两个服务，对应两个 VPCE，LiteLLM 里有三种写法：

```yaml
model_list:
  # ① Claude 等 bedrock/ 模型（Converse / Invoke）→ bedrock-runtime VPCE
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-6
      aws_region_name: us-west-2
      aws_bedrock_runtime_endpoint: "https://vpce-<id>-<hash>.bedrock-runtime.us-west-2.vpce.amazonaws.com"

  # ② GPT 走 Runtime 的 OpenAI 兼容层 → bedrock-runtime VPCE + /openai/v1
  - model_name: gpt-6.2-sol
    litellm_params:
      model: bedrock_mantle/openai.gpt-6.2-sol
      aws_region_name: us-west-2
      api_base: "https://vpce-<id>-<hash>.bedrock-runtime.us-west-2.vpce.amazonaws.com/openai/v1"
      extra_body: {model: global.openai.gpt-6.2-sol}   # Runtime 需要 global. 推理配置文件 ID
    model_info:
      mode: responses
      use_openai_responses_path: true

  # ③ Mantle 上的模型 → bedrock-mantle VPCE + /v1
  - model_name: gpt-5.6-luna
    litellm_params:
      model: bedrock_mantle/openai.gpt-5.6-luna
      aws_region_name: us-west-2
      api_base: "https://vpce-<id>-<hash>.bedrock-mantle.us-west-2.vpce.amazonaws.com/v1"
    model_info:
      mode: responses
```

实测结果：

| 模型 | 链路 | 结果 |
|---|---|---|
| claude-sonnet-4-6 | runtime VPCE / Converse | ✅ 非流式 + 流式 |
| gpt-6.2-sol | runtime VPCE / `/openai/v1/responses` | ✅ |
| gpt-5.6-luna | mantle VPCE / `/v1/responses` | ✅ |

抓包确认：LiteLLM 容器所有 443 出向连接的目标都是 VPCE 私网 IP，没有公网 IP。

### 全局写法（可选）

`bedrock/` 模型可以用环境变量统一覆盖，不用每个模型都写：

```
AWS_BEDROCK_RUNTIME_ENDPOINT=https://vpce-<id>-<hash>.bedrock-runtime.us-west-2.vpce.amazonaws.com
```

> 源码 `litellm/llms/bedrock/common_utils.py`、`base_aws_llm.py` 中有读取逻辑。这个写法只对 `bedrock/` 生效；`bedrock_mantle/` 模型仍需逐个写 `api_base`。

### 为什么换了域名签名还能过

- SigV4 签名用的 region 来自 `aws_region_name`，service 名固定是 `bedrock` 或 `bedrock-mantle`，跟 Host 是什么域名无关。
- VPCE 专属域名有 AWS 签发的证书，TLS 校验正常通过，**不需要** `ssl_verify: false`。

---

## 3. 方案对比：怎么让 IDC 访问到 VPCE

| 方案 | 做法 | 优点 | 代价 / 风险 | 建议 |
|---|---|---|---|---|
| **A. 配置写 VPCE 专属域名** | 如上第 2 节 | 零 DNS 改动，零额外资源 | 需要改应用配置 | ✅ **首选**（LiteLLM 支持） |
| B. Route 53 Resolver Inbound Endpoint | IDC DNS 把 `bedrock-runtime.us-west-2.amazonaws.com`、`bedrock-mantle.us-west-2.api.aws` 条件转发到 Inbound Endpoint | 应用零改动，AWS 官方标准方案 | 额外收费（每个 ENI 按小时计费，至少 2 个） | 应用改不了 endpoint 时用 |
| C. IDC DNS 加 CNAME | 标准域名 CNAME 到 VPCE 专属域名 | 零 AWS 资源 | 要维护 IDC 的 DNS zone | 可选 |
| D. hosts / A 记录写死 172.x | — | 简单 | VPCE 重建或子网变化后 IP 会变，静默失败；也丢了多 AZ 故障切换 | ❌ 不建议 |

---

## 4. 落地检查清单

1. **路由**：IDC ↔ VPC CIDR 经 DX（VGW / DXGW）双向可达。
2. **安全组**：VPCE 的 SG 放行 **IDC 网段 → TCP 443**。这条最容易漏；同 VPC 内常靠 SG 自引用规则放通，IDC 来的流量不在其中。
3. **凭证**：IDC 没有 EC2 Role，三选一：
   - IAM User 的 AK/SK（最小权限：`bedrock:InvokeModel*` 等）
   - **IAM Roles Anywhere**（证书换临时凭证）。注意要调用 STS，需再建 `com.amazonaws.<region>.sts` VPCE，否则 STS 调用仍走公网。
   - **Bedrock API Key（Bearer Token）**：runtime 的 `/openai/v1` 和 mantle 都支持
4. **VPCE Policy**：推荐 `Principal: *` + `Condition: aws:PrincipalAccount = <账号ID>`。用 API Key 时，Principal 对应的是 `BedrockAPIKey-xxxx` IAM User。
5. **区域一致**：`aws_region_name` 必须等于 VPCE 所在区域；VPCE 不能跨区域使用。

---

## 5. 常见坑

| 现象 | 原因 |
|---|---|
| `UnknownOperationException` | runtime VPCE 上用了 `/v1/responses`，应该用 `/openai/v1/responses` |
| mantle 返回 404 model not found | mantle 模型名不带 `global.` 前缀；可以先 GET `/v1/models` 看看实际有哪些模型 |
| 签名错误 / `SignatureDoesNotMatch` | `aws_region_name` 与 VPCE 区域不一致，或 service 名用错 |
| 连接超时 | SG 没放 IDC 网段的 443，或 DX 路由没通 |
| `no VPC endpoint policy allows ...` | VPCE Policy 的 Principal 或账号条件没覆盖到调用方 |
| 以为走了内网实际走公网 | 用了标准域名，而 IDC 侧没做 DNS 转发 → 解析到公网 IP |

### 验证是否走内网

```bash
# 1. 解析结果应是私网 IP
dig +short vpce-<id>-<hash>.bedrock-runtime.us-west-2.vpce.amazonaws.com

# 2. 抓 LiteLLM 出向 443 的 SYN 包，目标应全部是 VPCE 私网 IP
tcpdump -nn 'tcp port 443 and tcp[tcpflags] & tcp-syn != 0 and tcp[tcpflags] & tcp-ack == 0'
```
