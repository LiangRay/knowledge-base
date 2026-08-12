# 大厂实践拆解：Harness Engineering 完整落地路径

> **来源**：腾讯云开发者公众号（QQ音乐商业化团队）  
> **作者**：黄欣欣  
> **原文**：https://mp.weixin.qq.com/s/4yHC1kpl6o3P6TEVllaz8Q  
> **学习日期**：2025-08-12  
> **核心问题**：如何把 AI 协作从对话式编码，升级为可控、可审计、可复用的工程过程？

---

## 一、核心矛盾与动机

### 1.1 AI 时代软件工程的核心矛盾

```
生成快 + 验证慢 + 错误累积 = AI 时代软件工程的核心矛盾
```

AI 辅助编程三代演进：代码补全 → 对话式编码（Chat）→ 自主式 Agent。每一代提升 AI 自主性，同时暴露更深层问题。

### 1.2 Vibe Coding 的三个结构性缺陷

| 维度 | Vibe Coding 表现 | 生产级工程要求 |
|------|-----------------|--------------|
| **信息损耗** | 同一句话多次执行给出不同实现；AI 按自己理解"猜"需求 | 需求→设计→代码每步都要有显式产出和可追溯关系 |
| **知识孤岛** | AI 只知训练语料里的通用知识，不懂团队历史决策和私有约束 | 团队知识需要被持久化为 AI 可消费、可演进的工程制品 |
| **验证断档** | "能跑"就直接提交，概率性错误顺着 MR 滑进主干 | 每个关键节点都要有可机读的质量门禁和审计记录 |

### 1.3 核心公式

```
代码产出 = AI 能力 × 上下文质量
```

**为什么是乘法不是加法**：当上下文质量趋近于零时，模型再强，产出也是零。提升上下文质量是比提升模型能力更高效的杠杆——因为模型能力依赖外部厂商，而上下文质量完全掌握在团队自己手中。

### 1.4 真实业务仓的五类上下文缺口

| 缺口类型 | 典型问题 | AI 的"盲区" |
|---------|---------|------------|
| 隐性规范 | 团队约定的锁机制、埋点规则、错误码空间 | 不知道这些规范存在 |
| 历史决策 | "为什么当时选了 A 方案不选 B" | 训练语料里没有团队内部决策记录 |
| 服务契约 | IDL 字段的冻结状态、下游是否强依赖 | 看到文本，不理解哪些字段动不得 |
| 跨服务依赖 | 同一个需求要改哪几个服务、谁调谁 | 缺乏全局视角，不知道改动影响面 |
| 演进轨迹 | 某个模块上次大改的坑、灰度策略 | 没有跨会话记忆，无法继承团队经验 |

---

## 二、什么是 Harness Engineering

### 2.1 "Harness" 隐喻

```
原始 LLM = 一匹烈马（能力强、速度快、理解广，但方向不定、走不了远路、没有持久记忆）

加上 Harness（挽具）= 工具编排 / 记忆 / 沙箱 / 校验 / 反馈 / 上下文工程 / 生命周期 / 人机协同

→ 一个能稳定完成复杂任务的 Agent
```

**核心理念**：AI 参与问题分析、方案设计、编码实现、审查和验证，但最终判断权始终留在工程师手中。

- Vibe Coding 底层逻辑："让 AI 尽量自由地生成"
- Harness Engineering 底层逻辑："让 AI 在正确的轨道上尽量高效地生成"

### 2.2 业界共识的四大子系统

| 子系统 | 职责 |
|--------|------|
| ① 运行时控制系统 | 工具编排、状态持久化、错误恢复、反馈循环 |
| ② 上下文工程 | Context Window 优化、动态检索/摘要、防 Context Rot、信息优先级 |
| ③ 工具集成与防护 | API 调用标准化、预执行校验、阻止幻觉执行、安全护栏 |
| ④ 生命周期管理 | 多步长任务、Checkpoint/Crash Recovery、Human-in-the-Loop、跨会话状态 |

### 2.3 QQ音乐的扩展：Team Agent Governance

```
Team Agent Governance = Multi-Agent × Multi-Service × Multi-Lifecycle
（让团队的 AI 协作跨会话、跨工具、跨服务可治理）
```

---

## 三、L5 工程治理层定位

```
┌──────────────────────────────────────────────────────────┐
│ L5  Harness Engineering：团队拥有的工程治理层              │
│     - 五阶段流程                                          │
│     - 四道门禁                                            │
│     - 三层知识体系                                        │
│     - 服务矩阵                                            │
│     - Self-Refinement                                    │
│     - 多运行时适配                                        │
├──────────────────────────────────────────────────────────┤
│ L3/L4 执行层：                                            │
│     - 代码阅读 / 文件编辑 / 命令执行 / 测试修复            │
├──────────────────────────────────────────────────────────┤
│ L1/L2 体验层：IDE、补全、对话、diff 可视化                  │
└──────────────────────────────────────────────────────────┘
```

**边界清晰**：不替代执行工具，只定义执行工具必须遵守的工程上下文和协作协议。

### 为什么必须自研

通用产品无法替团队定义：
1. **服务矩阵语义** — 哪些服务属于同一业务域，依赖关系，IDL 路径
2. **需求生命周期语义** — 阶段、门禁、产物、追溯关系
3. **IDL 契约语义** — 字段冻结状态、兼容性、三仓分支联动
4. **团队经验语义** — 分页无上限打爆下游、goroutine 泄漏等特定经验
5. **工具解耦语义** — 规范保存在 `.codebuddy/`，渲染到不同 CLI 本地目录

---

## 四、亮点一：五阶段 + 四门禁

### 4.1 主流程

```
阶段1        阶段2⭐        阶段3⭐         阶段4⭐⭐        阶段5
初始化  →  需求定义  →  设计  →  开发  →  交付
           (评审门禁)   (设计门禁)  (Dev门禁+服务检查门禁)
```

### 4.2 四道门禁详情

| 门禁 | 位置 | 阻塞条件 |
|------|------|---------|
| 需求评审门禁 | 阶段 2.2 | 需求文档不合格 / 评审未通过 |
| 设计门禁 | 阶段 3.3 | 设计评审未通过 / 追溯链不达标 |
| Dev 进入门禁 | 阶段 4.2 | tasks/features.json 缺失或不合法 |
| 服务仓库检查门禁 | 阶段 4.3 | 三仓分支不一致 / 服务仓库未就位 |

### 4.3 设计原则

**错误越早被拦住，代价越低**：

- 阶段 2 拦住 → 改几行文档
- 阶段 3 拦住 → 改设计文档
- 阶段 4.1 拦住 → 改任务拆分
- 阶段 4.3 拦住 → 重切分支改环境
- 阶段 4.4 才发现 → 回滚代码 + 回滚 IDL + 回滚数据迁移

**门禁要"尽量少、尽量靠前"**：分别对应"意图、方案、任务、环境"四个最容易出大错、改动代价又最低的节点。

### 4.4 门禁是"机读"的

每个门禁都有对应的 Agent / Skill：
- `requirement-quality-reviewer` Agent（需求评审门禁）
- `detail-design-quality-reviewer` Agent（设计门禁）
- `traceability-gate-checker` Skill（追溯链校验）
- `service-repo-check.md`（服务仓库检查门禁）

门禁结论写入文件，采用固定格式，确保可读、可审计。

---

## 五、亮点二：三层知识体系 + 三仓联动

### 5.1 三层知识架构

| 层级 | 位置 | 范围 | 典型内容 | 更新频率 |
|------|------|------|---------|---------|
| 团队级 | `context/team/` | 所有项目必须遵循 | Git 规范、错误码空间、日志规范 | 最稳定 |
| 框架工程级 | `context/harness-framework/` | 所有需求研发必须遵循 | 五阶段流程、门禁规则、文档模板 | 中频 |
| 服务级 | `context/project/{project}/{module}/{service}/` | 特定服务 | 架构图、API、运维手册、踩坑经验 | 高频 |

**检索方式**：AI 按"团队 → 项目 → 模块 → 服务"逐层缩小范围，O(1) 命中。每层都有 `INDEX.md` 作为入口。

### 5.2 service-matrix/dependencies.yaml（单一真相源）

```yaml
workspace: ".."
business_repo: "music_commercial_go_proj"
idl_repo: "qqmusicjce"
default_team: "music-commercial"

teams:
  music-commercial:
    business_repo: "music_commercial_go_proj"
    idl_repo: "qqmusicjce"

modules:
  vip:
    team: music-commercial
    name: 会员核心域

services:
  vipapi:
    module: vip
    repo_path: "{business-repo}/vipapi"
    idl_required: true
  assetcardmallcgi:
    module: assetcard
    repo_path: "{business-repo}/assetcard/mall/assetcardmallcgi"
```

**设计特点**：
- 路径从不硬编码，用 `{business-repo}` / `{idl-repo}` 占位符
- 多团队共用同一 Harness 仓
- Active Team 三级解析：`$HARNESS_TEAM` > `.harness/local.yaml` > `default_team`
- CI 校验脚本保证占位符能正确解析

实际管理 **57 个服务**，路径深度 1-3 级不等，框架不对深度做过强假设。

### 5.3 三仓联动

```
一条 TAPD 单 T12345
         │
    ┌─────┼─────┐
    ▼     ▼     ▼
Harness仓  业务代码仓  IDL契约仓
 (脑)       (手脚)     (神经)

三个仓用完全相同的分支名：feature/Base/T12345
```

- 一条 TAPD 单 ID → 三仓分支名一对一，追溯链整洁
- 阶段 4.3 门禁自动校验三仓分支一致性
- 回滚时三仓同步处理

### 5.4 占位符词典（唯一真相源）

| 占位符 | 语义 | 举例 |
|--------|------|------|
| `{business-repo}` | 业务代码仓根的磁盘路径（绝对） | /data/workspace/music_commercial_go_proj |
| `{business-repo-name}` | 业务代码仓根的目录名 | music_commercial_go_proj |
| `{idl-repo}` / `{idl-repo-name}` | IDL 契约仓对称 | |
| `{project-name}` | 逻辑项目名 | music_commercial_go_proj |
| `{requirement-id}` | 需求 ID | minimal-requirement-practice |
| `{module-name}` / `{service-name}` | 业务模块 / 服务 | vip / vipapi |

---

## 六、亮点三：Skill / Agent / Command 三件套

### 6.1 三种能力原子分工

| 类型 | 定位 | 数量 | 调用方式 |
|------|------|------|---------|
| **Skill** | 可复用的工作流/规范/最佳实践 | 34 | 主对话按需 load，或被 Agent 调用 |
| **Agent** | 自主子任务执行者 | 24 | 主对话 Task 委派，或命令触发 |
| **Command** | 固定入口 + 标准化参数 | 35 | 用户输入 `/xxx:yyy` |

所有能力都是版本化 markdown 文件 — **Knowledge as Code**。

### 6.2 Agent 按阶段组织

```
.codebuddy/agents/
├── Init/                  项目初始化
├── RequirementManagement/ 需求管理
├── Startup/               阶段 1
├── Definition/            阶段 2（需求定义）
├── TechResearch/          阶段 3.1
├── OutlineDesign/         阶段 3.2
├── DetailDesign/          阶段 3.2
├── Implementation/        阶段 4.4（8 个审查 Agent）
├── Acceptance/            阶段 5
└── KnowledgeMaintenance/  知识沉淀
```

**代码审查 = 8 维度并行 Agent**：
1. 设计一致性检查
2. 复杂度检查
3. 并发安全检查
4. 错误处理检查
5. 安全漏洞检查
6. 契约一致性检查
7. 追溯性检查
8. 辅助检查

### 6.3 Skill 全景（34 个）

| 类别 | 代表 Skill |
|------|-----------|
| 需求生命周期 | managing-requirement-lifecycle、feature-lifecycle-manager |
| 文档撰写 | requirement-doc-writer、detail-design-doc-writer |
| 代码审查 | code-review-report、traceability-gate-checker |
| 服务治理 | service-dependency-analyzer、load-domain、load-service |
| 知识沉淀 | managing-knowledge、self-refinement |
| 工程工具 | dev-ocs、git-commit-message-generator、devops-cli |

`managing-requirement-lifecycle` 是核心调度 Skill，负责：意图识别、阶段检查、门禁验证、债务检查和计划更新。

### 6.4 Slash Command 示例

```
/requirement:new           # 新建需求
/requirement:continue      # 恢复上下文
/requirement:next          # 进入下一阶段
/requirement:gate-check    # 门禁自检
/req-task:list / start / context / done  # 任务流转
/agentic:code-review       # 多维度代码审查
/agentic:load-service      # 加载服务
/service:deps              # 查看依赖
/service:onboard           # 零配置接入外部服务
/knowledge:extract-experience  # 提取经验
/knowledge:generate-sop        # 生成 SOP
```

---

## 七、亮点四：Self-Refinement

### 7.1 闭环流程

```
① 用户纠正 AI 某个错误
      ↓
② AI 识别：这是"模式性教训"还是"一次性 diff"？
      ↓ （模式性）
③ AI 主动提议沉淀层级
   - 团队级 → context/team/
   - 框架工程级 → context/harness-framework/
   - 服务级 → context/project/{...}
      ↓ （用户确认）
④ 生成 experience 文档 / 更新 Skill / 修订规范
      ↓
⑤ 下次同类场景，AI 主动引用（新会话/新模型/新人也受益）
```

**错误不再"走一次算一次"，而是成为团队资产。**

### 7.2 产物示例

- `context/project/{project}/{module}/experience/*.md` — 踩坑经验
- `context/project/{project}/sop/*.md` — 从经验提炼出的标准操作规程
- `context/project/{project}/DEPENDENCY_ANALYSIS.md` — 子域依赖影响分析

---

## 八、与执行层工具的关系

### 8.1 定位

| 类型 | 代表 | 角色 |
|------|------|------|
| 执行层 | Claude Code / Cursor / Codex CLI / Gemini CLI / Continue | 提供 AI 能力、代码理解、文件编辑 |
| 治理层 | Harness Engineering | 定义流程、门禁、知识体系、服务矩阵 |

### 8.2 多运行时适配

```
.codebuddy/skills/ agents/ commands/  ← 真相源
         │
         ├── scripts/install.sh 渲染到：
         │
         ├── .claude/      ← Claude Code 读这个
         ├── .gemini/      ← Gemini CLI 读这个
         ├── .codex/       ← Codex CLI 读这个
         └── .continue/    ← Continue 读这个
```

这些是 gitignored 的镜像目录。修改规范时只改 `.codebuddy/`，不同 CLI 自动受益。

### 8.3 三句话总结关系

1. **执行交给工具**：读代码、改代码、跑测试、修复报错
2. **规则留在仓库**：流程、门禁、服务拓扑、团队知识和经验
3. **协议连接两者**：Skill/Agent/Command 把团队规范翻译成执行层可消费的上下文

---

## 九、工程制品全览

| 工程制品 | 作用 |
|---------|------|
| `AGENTS.md` | 全局协作规范和硬规则入口 |
| `.codebuddy/skills/` | 可复用能力单元（34 个） |
| `.codebuddy/agents/` | 专家角色定义（24 个） |
| `.codebuddy/commands/` | 标准化入口（35 个） |
| `context/team/` | 团队级规范 |
| `context/harness-framework/` | 框架工程规范 |
| `context/project/` | 服务级知识 |
| `.service-matrix/dependencies.yaml` | 服务拓扑与仓库路径 |
| `requirements/` | 需求生命周期产物 |
| `scripts/install.sh` | 多运行时渲染 |

**所有文件在仓库里** = 可 code review、可 diff、可回滚、可持续演进。

---

## 十、关键设计原则总结

1. **乘法思维** — 上下文质量是乘数因子，趋零则产出归零
2. **错误死在最便宜的地方** — 门禁设在代价拐点上
3. **Knowledge as Code** — 规范是文件，可 diff 可 review 可 rollback
4. **单一真相源** — 门禁口径收拢在一份文档，一次更新全仓生效
5. **执行层/治理层解耦** — 不被任何一个 AI 工具绑定
6. **渐进式披露** — AI 按层级逐步缩小知识范围，O(1) 命中
7. **Self-Refinement 闭环** — 团队知识不随会话消失

---

## 十一、个人思考与启发

### 对 Agent 门禁设计的启发

| 原则 | 文章实践 | 通用化 |
|------|---------|--------|
| 门禁要机读 | Agent + Skill 自动检查，结论写入文件 | 门禁结果必须是结构化数据，不是口头确认 |
| 门禁要少而精 | 4 道门禁对应"意图/方案/任务/环境" | 找到代价跃升的拐点，只在那里设门禁 |
| 知识要分层 | 团队级/框架级/服务级 | 按稳定性和适用范围分层，避免信息过载 |
| 经验要沉淀 | Self-Refinement → experience/ | 每次纠错都是提升"乘数因子"的机会 |
| 工具要解耦 | .codebuddy/ → 渲染到各 CLI | 规范和执行分离，避免 vendor lock-in |

### 适用场景

- **大仓多服务（Monorepo Microservices）** — 本文核心场景
- **跨团队协作** — 多团队共用 Harness 仓
- **高频迭代的业务系统** — 需要门禁防止错误累积
- **需要审计追溯的场景** — 金融、医疗、自动驾驶等

### 与得物 AI Harness 的对比

| 维度 | QQ音乐 Harness Engineering | 得物 AI Harness |
|------|---------------------------|----------------|
| 侧重 | 流程治理 + 知识工程 + 门禁 | PDCA 护栏 + Highway/ATV 混合架构 |
| 知识分层 | 三层 context/ | L1架构/L2设计/L3注释 |
| 经验沉淀 | Self-Refinement | 探索成功自动沉淀为确定性路径 |
| 执行层 | 复用 Claude Code/Codex/Gemini | 自研 Agent 框架 |
| 共同点 | 都强调"约束下的高效"，都把知识当作工程资产 | |
