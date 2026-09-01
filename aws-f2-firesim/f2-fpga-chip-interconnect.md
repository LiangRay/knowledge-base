# AWS F2 FPGA 芯片互联能力评估与 FireSim 多卡使用策略

> 日期：2026-08-31
> 场景：理想汽车 RISC-V 自研芯片 + 星环 OS 验证

## 1. 核心问题：F2 FPGA 能否测试芯片互联？

### 结论：硬件层面不支持 FPGA 间直连

AWS 官方在 GitHub Issue [#791](https://github.com/aws/aws-fpga/issues/791) 中明确回复：

> **"We currently do not support P2P over PCIe."** — DavidLiuAmzn (AWS)

相关讨论：[re:Post - F2 PCIe Peer-to-Peer FPGA-FPGA Connectivity](https://repost.aws/questions/QUxDMcF1IKRTq5haqCzEUlsA/f2-pcie-peer-to-peer-fpga-fpga-connectivity)

### F2 FPGA 互联限制详情

| 限制项 | 说明 |
|--------|------|
| GT 收发器 | 未暴露给用户逻辑，无法测试 SerDes 级别物理互联 |
| PCIe P2P | 不支持，每个 FPGA 是独立 PCIe 端点（x8 Gen4） |
| FPGA 间通信 | 唯一路径是通过主机 CPU 的 DPDK/SDE，延迟微秒级 |

**UCIe、CXL 等物理层（PHY）级别的芯片互联验证在 F2 上无法实现。**

### F2 实例规格

| 实例类型 | FPGA 数量 | FPGA 芯片 | vCPUs | FPGA 内存 | 系统内存 |
|----------|-----------|-----------|-------|-----------|----------|
| f2.4xlarge | 1 | VU47P (HBM) | 24 | 16 GiB HBM + 64 GiB DDR4 | 256 GiB |
| f2.12xlarge | 3 | VU47P (HBM) | 48 | 32 GiB HBM + 128 GiB DDR4 | 512 GiB |
| f2.48xlarge | 8 | VU47P (HBM) | 192 | 128 GiB HBM + 512 GiB DDR4 | 2,048 GiB |

### F1 vs F2 对比

| 特性 | F1 | F2 |
|------|----|----|
| FPGA 芯片 | Xilinx VU9P | Xilinx VU47P (**HBM**) |
| 最大 FPGA 数 | 8 (f1.16xlarge) | 8 (f2.48xlarge) |
| PCIe | x16 Gen3 | x8 Gen4 |
| HBM | ❌ | ✅ 16 GiB/FPGA |
| 网络 | 25 Gbps | 100 Gbps |
| FPGA 间直连 | 曾提及但从未公开支持 | **明确不支持** |

---

## 2. FireSim 软件层面的多芯片仿真能力

### 2.1 Supernode 模式 — 单 FPGA 多 SoC

- 单个 FPGA 上跑 4 个独立 RISC-V SoC 节点
- 节点间通过主机侧软件建模的 cycle-accurate 网络交换机通信
- 8 张卡 = **32 个仿真节点**，可模拟数据中心级集群
- 限制：目前仅支持 RocketChip + NIC 配置

### 2.2 FireAxe — 跨 FPGA 分区（ISCA 2024）

把单个大设计拆分到多个 FPGA 上，三种分区模式：

| 模式 | 精度 | 适用场景 |
|------|------|----------|
| Exact-Mode | 完全 cycle-exact | 有组合逻辑依赖的边界 |
| Fast-Mode | 插入 1 cycle 延迟，~2x 更快 | Ready-valid 接口 |
| NoC-Partition-Mode | 按 NoC 路由器自动切分 | Ring/Mesh 拓扑 |

⚠️ FireAxe 目前仅在本地 Alveo U250 上验证，尚未在 EC2 F2 上测试。

### 2.3 FireSim 可仿真的互联协议

| 协议 | 支持度 | 说明 |
|------|--------|------|
| NoC（片上网络） | ✅ 原生 | FireAxe NoC-Partition-Mode |
| TileLink/AXI 总线 | ✅ 通过 FireAxe | Ready-valid 接口可跨 FPGA |
| Ethernet | ✅ 原生 | 内建 cycle-accurate 网络仿真 |
| CXL / UCIe | ❌ 无内建 | 需自己写 RTL |
| Die-to-die / Chiplet | ⚠️ 理论可行 | 需自定义 RTL |

---

## 3. 多张 FPGA 的合理使用模式

既然 FPGA 间没有直连，多卡的核心思路是 **"独立并行"** 而非 **"协同互联"**。

### 3.1 独立并行加速（最常见）

每张 FPGA 跑相同或不同的加速逻辑，互不通信：
- 基因组学：8 张卡各处理不同 reads，吞吐量线性扩展
- 视频转码：每张卡处理一路视频流
- 金融风控：每张卡独立跑一组定价模型

### 3.2 FireSim 多节点仿真

- Supernode 模式：每张 FPGA 塞 4 个 RISC-V SoC
- 3 张卡 × 4 节点 = 12 个 RISC-V 核同时仿真
- 验证多核场景下 OS 调度行为

### 3.3 流水线架构（通过主机内存）

```
FPGA 0 (预处理) → Host Memory → FPGA 1 (推理) → Host Memory → FPGA 2 (后处理)
```

- 通过 DPDK/SDE 路径
- 延迟微秒级，不适合 cycle-accurate 场景
- 适合数据流水线（ETL、网络包处理链）

### 3.4 多设计并行验证

- 8 张卡同时跑 8 个不同的 RTL 设计变体
- 或同一设计的 8 组不同测试用例并行回归
- 大幅缩短验证周期

### 3.5 异构功能分工

每张卡承担不同角色，通过主机侧软件协调（如 0-1 网络加速，2-5 计算加速，6-7 存储加密）。

---

## 4. RISC-V 与 FireSim 的关系

### RISC-V 简介

RISC-V 是**开源免费的 CPU 指令集架构（ISA）**：
- 不依赖 ARM/Intel 授权，无地缘风险
- 可针对特定场景深度定制扩展指令
- 对中国芯片产业自主可控尤其重要

### FireSim 的角色

FireSim = 加州大学伯克利（RISC-V 发源地）开发的 **FPGA 加速仿真平台**：
- 把 RISC-V 芯片的 RTL 逻辑映射到 FPGA 上运行
- Cycle-accurate：每个时钟周期行为与真实芯片一致
- 自动生成外设模型（DRAM、网卡、磁盘）
- 流片前即可启动操作系统、跑驱动和应用

### 关键认知

**不需要客户提供芯片 RTL 即可做 Demo。** FireSim 内置现成的 RISC-V SoC 设计：

| 核 | 特点 | 已有 AGFI |
|----|------|-----------|
| RocketChip | 顺序执行，稳定成熟 | `agfi-0c5eedfce0e568971` (QuadRocket) |
| BOOM | 乱序执行，更接近高性能芯片 | `agfi-0ec7ae76159d3bfb6` |

---

## 5. 理想汽车场景分析

### 理想的技术栈

```
应用层（导航、座舱UI、ADAS）
        ↓
  星环 OS（理想自研操作系统）
        ↓
  RISC-V 芯片（理想自研硬件）
        ↓
      车辆
```

### FireSim 能验证什么

| 场景 | 可行性 | 说明 |
|------|--------|------|
| 单芯片功能验证 | ✅ | FireSim 核心能力 |
| 多核 SoC 内部 NoC 验证 | ✅ | FireAxe 支持 |
| OS + 驱动联调 | ✅ | FireSim 强项，cycle-accurate |
| 物理层芯片互联（UCIe/CXL PHY） | ❌ | F2 硬件限制 |
| 多芯片逻辑级互联 | ⚠️ | 需自写 RTL，FireAxe 在 F2 上未成熟 |

### Demo 策略

**第一步（无需理想配合）：**
- FireSim + RocketChip/BOOM → 启动 Linux → 展示"数字孪生"能力
- 使用已有的 AGFI，F2 实例 `i-0876fcc35b0e86342`

**第二步（客户感兴趣后）：**
- 理想提供自研芯片 RTL → 替换进 FireSim
- 星环 OS 直接跑在 FPGA 上验证
- 流片前完成软硬件全栈验证

### 话术建议

> "我们先用开源 RISC-V 核演示整个流程——FPGA 上启动 Linux、跑 benchmark、抓性能数据。你们只需要把自己的芯片 RTL 替换进来，星环 OS 就能在流片前跑起来验证。"

---

## 6. 替代方案

如果客户核心需求是**芯片间物理互联验证**，需要本地 FPGA 原型平台：
- Synopsys HAPS
- Cadence Protium
- 自定义多 FPGA 板卡（GT 链路直连）

F2 的价值定位应聚焦在：**单芯片/SoC 数字孪生 + 软件联调**，避免强调芯片互联。
