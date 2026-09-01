# FireSim on AWS F2：开源 RISC-V SoC 端到端仿真指南

> 日期：2026-08-31
> 目标：在 AWS F2 FPGA 实例上，使用全开源 RISC-V 技术栈完成从环境搭建到 Linux 启动的完整流程
> 版本：Chipyard v1.14.0 + FireSim v1.21.0（首个支持 F2 的版本）

---

## 目录

- [0. 全景图：端到端流程概览](#0-全景图端到端流程概览)
- [1. 技术栈说明](#1-技术栈说明)
- [2. 前置条件](#2-前置条件)
- [3. Phase 1：基础设施准备](#3-phase-1基础设施准备)
- [4. Phase 2：启动 F2 实例](#4-phase-2启动-f2-实例)
- [5. Phase 3：环境搭建](#5-phase-3环境搭建)
- [6. Phase 4：构建 Linux 工作负载](#6-phase-4构建-linux-工作负载)
- [7. Phase 5：配置仿真参数](#7-phase-5配置仿真参数)
- [8. Phase 6：加载 AGFI 并运行仿真](#8-phase-6加载-agfi-并运行仿真)
- [9. Phase 7：交互与验证](#9-phase-7交互与验证)
- [10. Phase 8：性能数据采集](#10-phase-8性能数据采集)
- [11. 进阶：自定义 SoC 配置](#11-进阶自定义-soc-配置)
- [12. 进阶：构建自定义 AGFI](#12-进阶构建自定义-agfi)
- [13. 成本管理](#13-成本管理)
- [14. 常见问题与排错](#14-常见问题与排错)
- [15. 参考资料](#15-参考资料)

---

## 0. 全景图：端到端流程概览

```
┌─────────────────────────────────────────────────────────────┐
│                    端到端流程（预计 2-3 小时）                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ① 基础设施准备        ② 启动 F2 实例      ③ 环境搭建         │
│  (KeyPair/SG/IAM)      (f2.6xlarge)       (conda + Chipyard)│
│       10 min               5 min             30-60 min       │
│         │                    │                   │           │
│         ▼                    ▼                   ▼           │
│  ④ 构建 Linux 工作负载   ⑤ 配置仿真参数     ⑥ 加载 AGFI       │
│  (FireMarshal)          (YAML 配置)        (预构建镜像)       │
│       10-15 min             5 min              1 min         │
│         │                    │                   │           │
│         ▼                    ▼                   ▼           │
│  ⑦ 运行仿真 & 交互      ⑧ 性能数据采集      ⑨ 停机/清理       │
│  (infrasetup + boot)    (AutoCounter等)     (stop instance)  │
│       5-10 min           按需配置               1 min         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**捷径**：如果使用预构建 AGFI（推荐），可以跳过自定义 AGFI 构建（节省数小时）。

---

## 1. 技术栈说明

整个链路全部开源，无需任何商业 IP 授权：

```
┌─────────────────────────────────────────┐
│  Linux Kernel (开源)                     │  ← 标准 RISC-V Linux
├─────────────────────────────────────────┤
│  FireMarshal (开源)                      │  ← 构建 Linux 根文件系统
├─────────────────────────────────────────┤
│  RISC-V CPU 核 (开源)                    │  ← RocketChip / BOOM / CVA6
├─────────────────────────────────────────┤
│  Chipyard SoC 框架 (开源)                │  ← 伯克利出品，搭积木式组 SoC
├─────────────────────────────────────────┤
│  FireSim 仿真平台 (开源)                  │  ← Cycle-accurate FPGA 仿真
├─────────────────────────────────────────┤
│  AWS F2 FPGA 实例                        │  ← Xilinx VU47P + HBM
└─────────────────────────────────────────┘
```

### 可选的开源 CPU 核

| 核 | 类型 | 特点 | 预构建 AGFI |
|----|------|------|------------|
| **RocketChip** (Quad) | 顺序执行，4核 | 最稳定，入门首选 | `agfi-0c5eedfce0e568971` |
| **BOOM** (Large) | 乱序超标量 | 高性能，类似 ARM A7x | `agfi-0ec7ae76159d3bfb6` |
| **MegaBOOM** | 乱序，更宽 | 最高性能 | 有预构建配置 |
| **CVA6 (Ariane)** | 顺序6级流水线 | 欧洲开源，工业级 | 有预构建配置 |
| **Gemmini** | Rocket + AI加速器 | 矩阵运算加速 | 有预构建配置 |
| **Saturn** | Rocket + 向量扩展 | RISC-V V 扩展 | 有预构建配置 |

---

## 2. 前置条件

### 2.1 AWS 账户要求

- [ ] F2 实例 vCPU 配额：检查 "Running On-Demand F instances"（至少 24 vCPU）
- [ ] FPGA Developer AMI 订阅：在 [AWS Marketplace](https://aws.amazon.com/marketplace) 搜索 "FPGA Developer AMI"，**必须点击 "Accept Terms"**
- [ ] IAM 权限：EC2、S3、IAM（如需构建 AGFI 还需 `ec2:CreateFpgaImage`）

```bash
# 检查 F2 可用区（目前确认 us-east-1b 可用）
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters "Name=instance-type,Values=f2.*" \
  --region us-east-1 \
  --query 'InstanceTypeOfferings[].{Type:InstanceType,AZ:Location}' \
  --output table

# 检查 vCPU 配额
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-74FC7D96 \
  --region us-east-1 \
  --query 'Quota.Value'
```

### 2.2 本地客户端要求

- AWS CLI v2（已配置凭证）
- SSH 客户端
- Session Manager 插件（如需 SSM 通道访问）

### 2.3 当前已有资源（我们的环境）

| 资源 | 值 |
|------|-----|
| 实例 ID | `i-0876fcc35b0e86342` (f2.6xlarge, us-east-1) |
| AMI | `ami-0b655f161063b404a` (FPGA Dev AMI 1.16.2, Vivado 2024.1) |
| KeyPair | `firesim-f2` |
| Security Group | `sg-0b9462001ddc199b4` |
| IAM Profile | `FireSim-F2-Profile` |
| EBS | 200GB gp3 |
| 已加载 AGFI | `agfi-0c5eedfce0e568971` (QuadRocket) |

---

## 3. Phase 1：基础设施准备

> 如果已有上述资源，可跳过此阶段。

### 3.1 创建 SSH 密钥对

```bash
aws ec2 import-key-pair \
  --key-name firesim-f2 \
  --public-key-material fileb://~/.ssh/id_ed25519.pub \
  --region us-east-1
```

### 3.2 创建安全组

```bash
# 创建安全组
SG_ID=$(aws ec2 create-security-group \
  --group-name firesim-f2-sg \
  --description "FireSim F2 FPGA" \
  --vpc-id <vpc-id> \
  --region us-east-1 \
  --query 'GroupId' --output text)

# 添加 SSH 入站规则（如使用 SSM 则不需要）
aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 \
  --cidr $(curl -s ifconfig.me)/32 \
  --region us-east-1
```

### 3.3 创建 IAM 角色

```bash
# 创建角色
aws iam create-role --role-name FireSim-F2-Role \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# 附加策略（S3 用于 AGFI 构建，SSM 用于远程管理）
aws iam attach-role-policy --role-name FireSim-F2-Role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name FireSim-F2-Role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# 创建实例配置文件
aws iam create-instance-profile --instance-profile-name FireSim-F2-Profile
aws iam add-role-to-instance-profile \
  --instance-profile-name FireSim-F2-Profile \
  --role-name FireSim-F2-Role
```

---

## 4. Phase 2：启动 F2 实例

```bash
aws ec2 run-instances \
  --image-id ami-0b655f161063b404a \
  --instance-type f2.6xlarge \
  --key-name firesim-f2 \
  --security-group-ids sg-0b9462001ddc199b4 \
  --subnet-id <subnet-in-us-east-1b> \
  --iam-instance-profile Name=FireSim-F2-Profile \
  --block-device-mappings '[{
    "DeviceName": "/dev/sda1",
    "Ebs": {"VolumeSize": 200, "VolumeType": "gp3"}
  }]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=FireSim-F2}]' \
  --region us-east-1
```

### 实例规格选择

| 实例类型 | FPGA | vCPU | 内存 | 价格/小时 | 建议用途 |
|----------|------|------|------|----------|---------|
| **f2.6xlarge** | 1 | 24 | 256 GiB | $1.98 | ✅ 单核/四核仿真，Demo 首选 |
| f2.12xlarge | 3 | 48 | 512 GiB | $3.96 | 多设计并行验证 |
| f2.48xlarge | 8 | 192 | 2 TiB | $15.84 | 大规模并行 / Supernode |

---

## 5. Phase 3：环境搭建

SSH 登录后执行：

### 5.1 安装 Conda

```bash
wget -q https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
  -O /tmp/miniforge.sh
bash /tmp/miniforge.sh -b -p $HOME/miniforge3
eval "$($HOME/miniforge3/bin/conda shell.bash hook)"
conda init bash
source ~/.bashrc
```

### 5.2 克隆 Chipyard（含 FireSim 子模块）

```bash
git clone https://github.com/ucb-bar/chipyard.git
cd chipyard
git checkout 1.14.0   # 最新稳定版，首个支持 F2 的版本
```

### 5.3 运行 build-setup（⏱️ 30-60 分钟）

```bash
# 确保 Vivado 在 PATH 中
source /opt/Xilinx/Vivado/2024.1/settings64.sh

# 运行构建（会安装所有依赖、编译工具链）
./build-setup.sh
```

> ⚠️ 这一步最耗时，会编译 RISC-V 工具链、Verilator 等。建议使用 `screen` 或 `tmux` 防止断连。

### 5.4 激活 FireSim 环境

```bash
# 每次新 SSH 会话都需要执行
eval "$($HOME/miniforge3/bin/conda shell.bash hook)"
cd ~/chipyard/sims/firesim
source sourceme-manager.sh
```

验证：
```bash
which firesim
# 应输出: /home/ubuntu/chipyard/sims/firesim/deploy/firesim
```

### 5.5 FireSim 管理器初始化

```bash
firesim managerinit --platform f2
```

这会生成配置文件模板到 `deploy/` 目录。

---

## 6. Phase 4：构建 Linux 工作负载

FireMarshal 负责构建运行在仿真 RISC-V SoC 上的 Linux 系统。

### 6.1 构建基础 Buildroot Linux（⏱️ 10-15 分钟）

```bash
cd ~/chipyard/software/firemarshal

# 构建基础镜像
./marshal -v build br-base.json

# 安装到 FireSim 工作负载目录
./marshal -v install br-base.json
```

产出物：
```
images/firechip/br-base/br-base-bin    # bootloader + Linux 内核
images/firechip/br-base/br-base.img    # 根文件系统
```

### 6.2 工作负载说明

| 工作负载 | 说明 | 用途 |
|----------|------|------|
| `br-base-uniform.json` | 所有节点跑相同的 Buildroot Linux | 基础 Demo |
| `br-base.json` | 单节点 Buildroot Linux | 单核测试 |
| 自定义 JSON | 可指定 overlay 文件系统、启动脚本 | 跑 benchmark 等 |

### 6.3 自定义工作负载（可选）

创建 `my-workload.json`：

```json
{
  "name": "my-workload",
  "base": "br-base.json",
  "overlay": "my-overlay/",
  "run": "my-run-script.sh"
}
```

在 `my-overlay/` 目录放入你要复制到根文件系统的文件，`my-run-script.sh` 会在 Linux 启动后自动执行。

---

## 7. Phase 5：配置仿真参数

需要编辑三个 YAML 配置文件：

### 7.1 硬件数据库 `deploy/config_hwdb.yaml`

定义 AGFI 与硬件配置的映射：

```yaml
# 预构建的 QuadRocket 配置（推荐 Demo 用）
firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3:
    agfi: agfi-0c5eedfce0e568971
    deploy_quintuplet_override: null
    custom_runtime_config: null
```

或使用 Chipyard 自带的示例：
```
~/chipyard/sims/firesim-staging/sample_config_hwdb.yaml
```

### 7.2 运行时配置 `deploy/config_runtime.yaml`

```yaml
run_farm:
  base_recipe: run-farm-recipes/externally_provisioned.yaml
  recipe_arg_overrides:
    run_farm_tag: firesim-f2-demo
    run_farm_hosts_to_use:
      - localhost: one_fpgas_spec    # 本地模式，1 个 FPGA

target_config:
    topology: no_net_config          # 无网络拓扑（单节点）
    no_net_num_nodes: 1              # 1 个仿真节点
    link_latency: 6405               # 网络延迟（无网络时忽略）
    switching_latency: 10            # 交换延迟（无网络时忽略）
    net_bandwidth: 200               # 网络带宽 Gbps（无网络时忽略）
    profile_interval: -1
    default_hw_config: firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3

tracing:
    enable: no                       # 启用指令 trace（按需开启）
    output_format: 0                 # 0=可读文本, 1=二进制, 2=flamegraph
    selector: 1
    start: 0
    end: -1

autocounter:
    read_rate: 0                     # 0=仿真结束时读取, N=每N周期读取

workload:
    workload_name: br-base-uniform.json
    terminate_on_completion: no      # no=仿真持续运行直到手动停止
    suffix_tag: null

host_debug:
    zero_out_dram: no
    disable_synth_asserts: no

synth_print:
    start: 0
    end: -1
    cycle_prefix: yes

metasimulation:
    metasimulation_enabled: false
    metasimulation_host_simulator: verilator
    metasimulation_only_plusargs: "+fesvr-step-size=128 +max-cycles=100000000"
    metasimulation_only_vcs_plusargs: "+vcs+initreg+0 +vcs+initmem+0"
```

### 7.3 构建配方（使用预构建 AGFI 时不需要修改）

预构建的 F2 配置清单：

| 配置名 | CPU 核 | NIC | FPGA 频率 |
|--------|--------|-----|----------|
| `firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3` | Quad Rocket | ❌ | 140 MHz |
| `firesim_rocket_quadcore_nic_l2_llc4mb_ddr3` | Quad Rocket | ✅ | 90 MHz |
| `firesim_boom_singlecore_no_nic_l2_llc4mb_ddr3` | Large BOOM v3 | ❌ | 65 MHz |
| `firesim_boom_singlecore_nic_l2_llc4mb_ddr3` | Large BOOM v3 | ✅ | 65 MHz |
| `firesim_megaboom_singlecore_no_nic_l2_llc4mb_ddr3` | MegaBOOM v3 | ❌ | 50 MHz |
| `firesim_rocket_singlecore_gemmini_no_nic_l2_llc4mb_ddr3` | Rocket + Gemmini AI | ❌ | 110 MHz |
| `firesim_saturn_REFV256D128_rocket_singlecore_no_nic_l2_llc4mb_ddr3` | Rocket + Saturn Vector | ❌ | 60 MHz |
| `firesim_cva6_singlecore_no_nic_l2_llc4mb_ddr3` | CVA6 (Ariane) | ❌ | 90 MHz |

---

## 8. Phase 6：加载 AGFI 并运行仿真

### 8.1 方式 A：使用 FireSim 管理器（推荐）

```bash
cd ~/chipyard/sims/firesim

# Step 1: 基础设施设置 — 编译驱动、加载 AGFI、配置 FPGA
firesim --platform f2 infrasetup

# Step 2: 启动仿真 — 引导 Linux
firesim --platform f2 boot
```

如果使用 Chipyard 示例配置：
```bash
firesim --platform f2 infrasetup \
  -a ~/chipyard/sims/firesim-staging/sample_config_hwdb.yaml \
  -r ~/chipyard/sims/firesim-staging/sample_config_build_recipes.yaml

firesim --platform f2 boot \
  -a ~/chipyard/sims/firesim-staging/sample_config_hwdb.yaml \
  -r ~/chipyard/sims/firesim-staging/sample_config_build_recipes.yaml
```

### 8.2 方式 B：手动加载 AGFI（调试用）

```bash
# 加载 AGFI 到 FPGA slot 0
sudo fpga-load-local-image -S 0 -I agfi-0c5eedfce0e568971

# 验证加载状态
sudo fpga-describe-local-image -S 0 -R -H
# 期望输出: StatusName=loaded, StatusCode=0, ErrorName=ok
```

### 8.3 `infrasetup` 做了什么

1. 编译 FPGA 软件驱动程序
2. 在目标节点安装 AWS FPGA SDK
3. 加载 XDMA 内核模块
4. 将 AGFI 烧录到 FPGA 槽位
5. 拷贝工作负载文件到目标节点

### 8.4 `boot` 做了什么

1. 启动仿真进程（后台 screen 会话）
2. 将 Linux 内核 + rootfs 加载到仿真 DRAM
3. 引导 RISC-V Linux 启动
4. 每 10 秒输出仿真状态

---

## 9. Phase 7：交互与验证

### 9.1 连接到仿真控制台

```bash
# 附加到仿真 UART 控制台
screen -r fsim0
```

### 9.2 Linux 交互

```bash
# 登录（用户名 root，无密码）
buildroot login: root

# 验证系统信息
uname -a
# Linux buildroot 4.15.0-rc6-... riscv64 GNU/Linux

# 查看 CPU 信息
cat /proc/cpuinfo

# 查看内存
free -h

# 运行简单 benchmark
time echo "scale=1000; 4*a(1)" | bc -l

# 关机（结束仿真）
poweroff -f
```

### 9.3 仿真输出

关机后会看到：
```
time elapsed: 468.8 s, simulation speed = 88.50 MHz
*** PASSED *** after 41492621244 cycles
```

- **simulation speed**: 仿真时钟频率（不等于目标芯片频率）
- **cycles**: 仿真经过的总时钟周期数

### 9.4 从 screen 断开（不终止仿真）

```
按 Ctrl+A，然后按 D
```

### 9.5 仿真结果收集

```bash
# 结果自动保存到
ls ~/chipyard/sims/firesim/deploy/results-workload/

# 包含：
# - uartlog          UART 控制台完整日志
# - memory_stats.csv 内存统计
# - *.csv            AutoCounter 数据（如已启用）
```

---

## 10. Phase 8：性能数据采集

### 10.1 AutoCounter — 硬件性能计数器

在 `config_runtime.yaml` 中配置：

```yaml
autocounter:
    read_rate: 0    # 0 = 仿真结束时一次性读取
                    # N = 每 N 个周期采样一次
```

自动采集的指标（Rocket/BOOM 内建）：
- **IPC** (Instructions Per Cycle)
- **Cache hit/miss rate** (L1I, L1D, L2)
- **Branch prediction** 命中率/误预测率
- **TLB miss** 率
- **内存带宽** 利用率

输出为 CSV 格式，列：`cycle, counter_name, value`

### 10.2 TracerV — 指令级 Trace

```yaml
tracing:
    enable: yes
    output_format: 0   # 0=可读文本, 1=二进制, 2=flamegraph
    selector: 1         # 1=按周期范围
    start: 0            # 起始周期
    end: -1             # -1=到结束
```

| 格式 | 用途 |
|------|------|
| 0 (文本) | 人工查看指令执行序列 |
| 1 (二进制) | 大规模分析，配合后处理工具 |
| **2 (flamegraph)** | 🔥 生成火焰图，直观看热点函数 |

### 10.3 Synthesized Printf

RTL 中的 `printf` 语句在 FPGA 上真实执行，不影响仿真精度：

```yaml
synth_print:
    start: 0
    end: -1
    cycle_prefix: yes   # 每行前加周期号
```

### 10.4 断言检查

```yaml
host_debug:
    disable_synth_asserts: no   # 开启 RTL 断言检查
```

RTL 中的 `assert` 语句会在 FPGA 上实时检查，触发时仿真暂停并报告。

---

## 11. 进阶：自定义 SoC 配置

### 11.1 Chipyard 配置系统

Chipyard 使用 Scala Config Fragment 堆叠式定义 SoC：

```
TARGET_CONFIG = FireSim桥接 + FireSim调优 + Chipyard基础配置
```

例如：
```
WithDefaultFireSimBridges_WithFireSimTestChipConfigTweaks_chipyard.QuadRocketConfig
```

### 11.2 可用的 Chipyard 基础配置

| 配置 | 说明 |
|------|------|
| `chipyard.RocketConfig` | 单核 Rocket |
| `chipyard.QuadRocketConfig` | 四核 Rocket |
| `chipyard.LargeBoomV3Config` | 单核大型 BOOM v3 |
| `chipyard.MegaBoomV3Config` | 单核 MegaBOOM（最高性能） |
| `chipyard.CVA6Config` | 单核 CVA6 |
| `chipyard.GemminiRocketConfig` | Rocket + Gemmini AI 加速器 |
| `chipyard.RocketConfig` + Saturn | Rocket + 向量扩展 |

### 11.3 PLATFORM_CONFIG 组件

| 组件 | 说明 |
|------|------|
| `BaseF2Config` | F2 平台基础配置（必选） |
| `FRFCFS16GBQuadRankLLC4MB` | DRAM 时序模型：FR-FCFS, 16GB, 4MB LLC |
| `WithAutoILA` | 集成逻辑分析仪（调试用） |
| `MCRams` | RAM 优化 |
| `WithPrintfSynthesis` | 启用综合 printf |

### 11.4 创建自定义 Scala 配置（永久）

编辑 `generators/firechip/chip/src/main/scala/TargetConfigs.scala`：

```scala
// 自定义：双核 Rocket + 大 L2 缓存
class FireSimDualRocketConfig extends Config(
  new WithDefaultFireSimBridges ++
  new WithFireSimConfigTweaks ++
  new chipyard.DualRocketConfig)
```

---

## 12. 进阶：构建自定义 AGFI

> ⚠️ 构建一个 AGFI 需要 **4-12 小时**，费用约 $10-30。首次 Demo 建议使用预构建 AGFI。

### 12.1 配置构建

编辑 `deploy/config_build.yaml`：

```yaml
builds_to_run:
    - firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3
```

### 12.2 构建流程

```bash
firesim --platform f2 buildbitstream \
  -r ~/chipyard/sims/firesim-staging/sample_config_build_recipes.yaml
```

构建流水线：
```
Chisel RTL → FIRRTL → Golden Gate (FAME-1 变换) → Verilog
    → Vivado 综合 → 布局布线 → 打包上传 S3 → AWS 转换为 AFI/AGFI
```

### 12.3 构建输出

- `deploy/results-build/<config_name>/AGFI_INFO` — 包含 AGFI ID
- 构建完成后自动发送邮件通知
- 将 AGFI ID 添加到 `config_hwdb.yaml`

---

## 13. 成本管理

### 13.1 费用估算

| 项目 | 费用 |
|------|------|
| f2.6xlarge 运行 | $1.98/小时 |
| 200GB gp3 EBS 存储 | ~$16/月（停机也收费） |
| AGFI 构建（z1d.2xlarge × 8h） | ~$10-30/次 |
| S3 存储（AGFI 中间产物） | < $1/月 |

### 13.2 省钱建议

- ✅ **不用时立即 STOP 实例**（`aws ec2 stop-instances`）
- ✅ 使用预构建 AGFI，避免重复构建
- ✅ Demo 验证完毕后缩减 EBS 到 100GB
- ⚠️ Spot 实例可节省 50-70%，但可能被中断

### 13.3 停机/启动命令

```bash
# 停机（保留 EBS 数据）
aws ec2 stop-instances --instance-ids i-0876fcc35b0e86342 --region us-east-1

# 启动
aws ec2 start-instances --instance-ids i-0876fcc35b0e86342 --region us-east-1

# 查看状态
aws ec2 describe-instances --instance-ids i-0876fcc35b0e86342 \
  --query 'Reservations[].Instances[].{State:State.Name,IP:PublicIpAddress}' \
  --region us-east-1
```

---

## 14. 常见问题与排错

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `OptInRequired` 启动失败 | 未订阅 FPGA Developer AMI | 在 Marketplace 点击 "Accept Terms" |
| `firesim: command not found` | 未激活环境 | 执行 `source sourceme-manager.sh` |
| FPGA 加载失败 | AGFI 不兼容 F2 | 确认 AGFI 基于 `BaseF2Config` 构建 |
| `AssertionError` 运行时崩溃 | 缺少 `metasimulation` 配置段 | 在 runtime YAML 中添加完整的 metasimulation 段 |
| Conda 不在 PATH | 新会话未初始化 | 执行 `eval "$($HOME/miniforge3/bin/conda shell.bash hook)"` |
| 构建超时 | Vivado 编译慢 | 使用更大实例（z1d.2xlarge 以上） |
| `screen -r fsim0` 无会话 | 仿真未启动或已结束 | 检查 `screen -ls`，重新 `firesim boot` |
| run_farm 配置错误 | 格式不对 | `run_farm_hosts_to_use` 要用 `- hostname: spec` 字典格式 |

---

## 15. 参考资料

### 官方文档
- [FireSim 文档](https://docs.fires.im/) — v1.21.0
- [Chipyard 文档](https://chipyard.readthedocs.io/) — v1.14.0
- [AWS FPGA HDK GitHub](https://github.com/aws/aws-fpga) — F2 分支

### 论文
- **ISCA 2018**: "FireSim: FPGA-Accelerated Cycle-Exact Scale-Out System Simulation in the Public Cloud"
- **ISCA 2024**: "FireAxe: Partitioned FPGA-Accelerated Simulation of Large-Scale RTL Designs"
- **ICCAD 2019**: "Golden Gate: Bridging The Resource-Efficiency Gap Between ASICs and FPGA Prototypes"

### 相关文档（本仓库）
- [F2 FPGA 芯片互联能力评估](./f2-fpga-chip-interconnect.md)

---

## Quick Start 速查

**假设环境已搭建完毕，最快启动仿真的命令序列：**

```bash
# 1. 激活环境
eval "$($HOME/miniforge3/bin/conda shell.bash hook)"
cd ~/chipyard/sims/firesim
source sourceme-manager.sh

# 2. 基础设施设置 + 启动
firesim --platform f2 infrasetup
firesim --platform f2 boot

# 3. 连接控制台
screen -r fsim0

# 4. 登录 (root, 无密码)
# uname -a  →  看到 riscv64 Linux！

# 5. 结束
poweroff -f         # 在仿真 Linux 内
# 或
firesim --platform f2 kill   # 从管理器强制终止
```
