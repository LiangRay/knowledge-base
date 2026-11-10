# 拆开一个 Agent Sandbox：架构、场景和形态

> 来源：微信公众号「架构探秘」·《拆开一个 Agent Sandbox：它的架构、场景和形态》（AI Agent 基础设施观察 · 系列第 2 篇）
> 原文：https://mp.weixin.qq.com/s/7BZrJo6ozp2KuOO6sy2fCg
> 整理时间：2026-11
>
> 前 1–7 节是原文要点整理；第 8 节「映射到 Google ACE / AWS AgentCore」是我们自己补充的分析。

---

## TL;DR

1. **先定 Agent 主循环放在哪**：住在沙箱里（Agent in Sandbox），还是在外面、把沙箱当工具房用（Agent Use Sandbox）。这个定了，架构问题就解决了一半。
2. **平台结构是「五层 + 三个侧栏」**：接入层、控制面、编排层、运行时、沙箱内，再加存储、受控出口、观测审计。控制面和数据通路要分开设计。
3. **听到「支持休眠」，先问保留的是什么**：文件、驻留内存、内存快照、克隆分支是四个档次，互相不能替代。
4. **光有隔离不够**：Agent 不用逃逸，拿着合法的 API Key 也能删库。出网策略和凭证权限必须在沙箱外部强制执行。

---

## 1. Agent 和沙箱，谁在里面？

| 模式 | 含义 | 适用场景 |
|---|---|---|
| **Agent in Sandbox** | 主进程、任务环境、代码仓库都在沙箱内 | 长时间驻留、持续执行、需要保持状态的 Agent，比如跑几天的编码任务、「数字员工」。空闲时可以休眠或打 checkpoint，等模型推理完或人回复后再唤醒，省掉空闲期的成本 |
| **Agent Use Sandbox** | Agent 本体跑在应用里，需要执行时临时拉起一个沙箱，用完即回收 | Agent Tools、代码解释器、Browser Use、数据处理、自动化测试。按需创建，天然隔离 |
| **叠加** | Agent 住在沙箱 A 里，每次工具调用再申请临时沙箱 B，规划在 A、执行在 B | 已有不少平台落地 |

**「仅隔离」不够**：Agent 不需要逃逸沙箱，用合法权限也可能把远端资源改错。比如拿着 API Key 调自己的接口删了库，这不算逃逸，属于正常使用。所以选沙箱除了看隔离强度，还要看**出网策略和凭证权限是不是在沙箱外部强制执行**。

---

## 2. 典型平台解剖图：五层 + 三侧栏

参考了 OpenSandbox、Perplexity SPACE、K8s SIG Agent Sandbox 公开的架构，各家的职责划分高度一致：

```
┌──────────────────────────────────────────────┐   ┌──────────┐
│ Agent 应用 / 工作流 / 评测系统                │   │ 存储      │ 镜像、工作区、快照
├──────────────────────────────────────────────┤   │          │ （沙箱会漂移，东西不能丢）
│ 接入层：原生 SDK · E2B 兼容 · MCP             │   ├──────────┤
├──────────────────────────────────────────────┤   │ 受控出口  │ 网络策略、身份核验、凭据代理
│ 控制面：鉴权 · 租户 · 模板 · 生命周期 · 配额   │   │          │ （真实钥匙放在沙箱外）
├──────────────────────────────────────────────┤   ├──────────┤
│ 编排层：调度 · 预热池 · 会话路由 · 回收        │   │ 观测审计  │ 事件、指标、追踪、
├──────────────────────────────────────────────┤   │          │ 拒绝/放行的证据
│ 运行时：容器 / gVisor / Kata / microVM        │   └──────────┘
├──────────────────────────────────────────────┤
│ 沙箱内：执行代理 · 代码解释器 · 浏览器 · 任务进程│
└──────────────────────────────────────────────┘
```

三个侧栏就是「仅隔离不够」这条原则在架构上的体现。

### 两个关键设计取舍

1. **控制面和数据通路分开**
   - 生命周期管理（创建、回收、策略）管「房间」本身。
   - 命令流、文件、终端、端口管「屋里发生的事」。
   - 分开设计后，扩展性、故障影响范围和权限边界可以分别评估。OpenSandbox 和 SPACE 都是这么做的。
2. **编排和隔离是两件事**
   - CRD、控制器、预热池本身证明不了 VM 级隔离或内存快照能力。
   - 反过来，gVisor / Kata 只提供底层隔离边界，不会自带租户、计费和审计。
   - 选型时把这两条线**分开验收**，可以避开大部分误判。

---

## 3. 四种沙箱形态（按 Agent 需要什么样的「手」分）

| 形态 | 提供的能力 | 典型场景 |
|---|---|---|
| 代码沙箱 | 命令、进程、文件、多语言运行时 | Coding Agent、代码解释器、数据分析、自动化测试 |
| 浏览器沙箱 | 无头或带 GUI 的浏览器 | Browser Agent、Deep Research、网页自动化 |
| 桌面沙箱 | 完整桌面 GUI | Computer Use Agent、办公任务、桌面软件自动化 |
| 移动沙箱 | 移动端应用环境 | 移动 Agent、应用自动化、兼容性测试 |

- 这几种形态可以组合。**AIO（All-In-One）Sandbox** 的做法是把浏览器、Shell、文件、VS Code、Jupyter 打进同一个镜像。
- 阿里云 Agent Sandbox 的产品页覆盖了全部四种形态。
- ⚠️ **Code Interpreter 不等于完整的沙箱平台**：执行一段代码、返回结构化结果，和长期持有文件、后台进程、支持会话重连，是两个档次的能力。

---

## 4. 场景 → 沙箱选型关注点

| 场景 | 需求 | 重点看 |
|---|---|---|
| 数据分析 / 代码执行 | 安全运行生成的代码，拿到结果文件 | 多语言模板、输出流、资源限制 |
| Coding Agent | 在独立工作区改代码、跑任务 | 持久文件、后台进程、端口、重连 |
| 浏览器 / 数字员工 | 隔离登录态，操作网页和桌面 | 会话隔离、截图下载、出口策略 |
| 长程 Agent | 中断后能继续，压低闲置资源占用 | TTL、续期、休眠、恢复一致性 |
| RL / 评测 | 海量并行、分支探索 | 快照克隆、批量创建、可复现性 |
| 企业内部平台 | 统一接入、数据权限可控 | 私有部署、租户配额、身份、审计 |

对架构要求最高的是**长程 Agent** 和 **RL / 评测**。它们同时要求**持久化、高密度、快速唤醒**，而这三点互相冲突：
- 要持久，状态就大；
- 要密度，资源就得释放；
- 要快，恢复就得在秒级完成。

---

## 5. 「暂停 / 恢复」：先搞清楚保留的是什么

业内说的「支持休眠」至少有四种含义，**互相不能替代**：

| 档位 | 保留的内容 | 说明 |
|---|---|---|
| ① 工作区持久化 | 只保留文件 | 进程和内存要重建，环境里装过的东西要重来 |
| ② 驻留暂停 | 停止执行，宿主机内存仍被占用 | 属于「假休眠」，不能当持久休眠卖 |
| ③ 内存快照休眠 | 内存和运行状态写成快照，计算资源真正释放 | 这才算真正「睡下去」 |
| ④ 快照克隆 / 分支 | 从一个状态派生多个新实例 | 身份、网络、随机数、凭据都要重新初始化。**RL 依赖的就是这一档** |

**各家命名混乱**：
- SPACE：驻留叫 `pause`，持久叫 `suspend`；它的 E2B 兼容层又把 E2B 的 `pause` 映射成 `suspend`。
- edvabe：「暂停」其实是 `docker pause`，「快照」其实是 `docker commit`，跟内存快照完全不是一回事。
- Firecracker 官方文档特别提醒：**恢复成功不代表网络连接还在**。数据库连接、浏览器登录态都要纳入恢复测试。

**看到「支持 pause / resume」时要追问三件事**：
1. 保留的是**文件**、**进程内存**，还是**应用自己保存的对话**？
2. 谁触发暂停？怎么唤醒？
3. 发生故障后能恢复到哪个时间点？

---

## 6. 选型四维度（Christian Posta,《Not All Agent Sandboxes Are Created Equally》）

| 维度 | 要问的问题 |
|---|---|
| 边界与访问控制 | 负载里有什么？能访问什么？谁控制权限？出网、凭证、策略是否在沙箱外部强制执行？ |
| 环境 | 需要哪些文件、工具、依赖和 Agent 配置？谁来维护环境？工作目录是挂载进来，还是独立检出、再回收变更？ |
| 生命周期 | 怎么启动、等待、停止、恢复？恢复的是文件、进程内存，还是应用保存的对话？ |
| 接口与体验 | 给谁用（开发者、应用还是平台团队）？断连后由谁负责执行、输出和清理？ |

### 验收动作（用负例测试）

用测试仓库、模拟凭证和合成数据跑以下几类：
- **策略**：访问未授权域名、访问跨租户资源。
- **恢复可靠性**：空闲后唤醒、断连、宿主机故障。
- **规模与成本**：批量创建 + 集中唤醒，记录 P95。

选型记录里要写清楚**策略、环境、恢复、审计分别由哪个组件负责**。这是让「仅隔离不够」真正落地的最后一道关。

---

## 7. 系列预告（原文）

下一篇：《Agent Sandbox 技术生态全景》。内容包括 L0–L3 分层、代表性开源项目，以及 E2B 协议为什么成了事实标准。

---

## 8. 补充分析：映射到 Google ACE / AWS AgentCore Runtime

> 本节不是原文内容。ACE 部分只依据 Google 的架构图解读，带「推断」的地方未经官方核实；AgentCore 部分依据 AWS 官方 devguide。

### 8.1 五层 + 三侧栏对照

| 层 / 侧栏 | Google Agent Compute Engine（架构图） | AWS AgentCore Runtime |
|---|---|---|
| 接入层 | Client library：控制面调用 + 数据面 gRPC | SDK / CLI，`InvokeAgentRuntime`，支持 MCP、A2A 协议 |
| 控制面 | Lifecycle API、Scheduler、Policy & identity、Metering | Runtime / Version / Endpoint 管理，IAM，Policy（Cedar） |
| 编排层 | 共享的多区域裸金属主机池，Scheduler 负责放置 microVM | 全托管；V2 版本用启动快照加速冷启动 |
| 运行时 | Cloud Hypervisor microVM | microVM（Firecracker 系），或 Instances（客户账号内的 EC2，支持 GPU） |
| 沙箱内 | 客户镜像 + guest agent，通过 VSOCK 命令通道连接 | 客户容器（ECR），通过 HTTP 协议契约（`/invocations`、`/ping`） |
| 存储侧栏 | Image cache、Snapshots、Hyperdisk | Session storage、EFS（Instances 还支持 EBS） |
| 受控出口侧栏 | Egress proxy → Gateway：出口策略 + **在网络层注入凭证，凭证不进 VM** | VPC 模式 + NAT / Network Firewall；Identity Token Vault；外部调用经 AgentCore Gateway 时凭证不进沙箱 |
| 观测审计侧栏 | Logs and metrics 回传给客户 | CloudWatch / OTel（AgentCore Observability） |

### 8.2 用「休眠四档」衡量

| 档位 | Google ACE | AgentCore Runtime |
|---|---|---|
| ① 工作区持久化 | Hyperdisk / 快照（推断） | ✅ Session storage / EFS |
| ② 驻留暂停 | 图上有 microVM「Paused」状态（具体是驻留还是快照，推断未知） | 会话空闲时保持，超过 idle timeout 回收 |
| ③ 内存快照休眠 | 图上有 Snapshot / restore / pause，看起来是用户可调用的 API | ❌ V2 快照只用于冷启动加速（每个版本打一次），用户不能调用 |
| ④ 快照克隆 / 分支 | 推断支持（这是 RL 场景的核心卖点） | ❌ 不支持。只能用「每类初始状态建一个版本」或复制 EFS 目录来近似，进程和内存状态无法保留 |

### 8.3 用「仅隔离不够」衡量凭证位置

| 凭证类型 | ACE | AgentCore |
|---|---|---|
| 云平台身份凭证 | 留在 VM 外，由 Gateway 注入 | 执行角色的 IAM 临时凭证**在 microVM 内** |
| 第三方 OAuth / API Key | 由 Gateway 在网络层注入 | Identity 从 Token Vault 取出后**进入 Agent 进程内存**；走 Gateway MCP 工具的凭证不进沙箱 |

**对客户的建议**（高安全场景，如金融）：
- 使用 VPC 模式，配合 Network Firewall 域名白名单；
- 外部调用一律走 AgentCore Gateway；
- 执行角色按最小权限配置；
- 在选型记录里如实写明残余风险：执行角色的凭证仍在 VM 内。

### 8.4 场景选型速查（AWS 视角）

| 场景 | 推荐 |
|---|---|
| 生产级 Agent 托管、长程 Agent（≤ 8h，Instances 最长 14 天） | AgentCore Runtime |
| 代码执行 / 浏览器工具（Agent Use Sandbox） | AgentCore Code Interpreter / Browser |
| RL rollout、需要快照分叉、海量短任务 | 目前 AgentCore 不合适。建议在 EKS 上用 Firecracker / Kata 自建，或用 E2B、Modal 等第三方 |
| 需要 GPU 的 Agent（渲染、本地小模型） | AgentCore Runtime Instances |

---

## 参考

- 原文：https://mp.weixin.qq.com/s/7BZrJo6ozp2KuOO6sy2fCg
- Christian Posta,《Not All Agent Sandboxes Are Created Equally》
- 文中提及：OpenSandbox、Perplexity SPACE、K8s SIG Agent Sandbox、AIO Sandbox、阿里云 Agent Sandbox、edvabe、E2B、Firecracker snapshot 文档
- AWS AgentCore devguide：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
