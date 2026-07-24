# FireSim on AWS F2 部署指南

## 概述

在 AWS F2 实例上部署 FireSim，使用预构建 AGFI 运行周期精确的 RISC-V 处理器仿真。

**目标**：加载预构建的 Rocket/BOOM RISC-V SoC 到 FPGA，运行 Linux 仿真并采集性能数据。

**适用场景**：自研 RISC-V 芯片 + OS 的联合性能验证（如理想星环 OS + 自研 RISC-V 核）。

---

## 前置条件

- AWS 账号，已订阅 [FPGA Developer AMI](https://aws.amazon.com/marketplace/pp?sku=e4txuxx6uz6371b7tgmotozac)
- us-east-1 区域，F2 vCPU 配额 ≥ 24
- IAM Role 具备 S3FullAccess（用于 AFI 创建）

---

## 第一阶段：基础设施准备

### 1.1 导入 SSH Key

```bash
aws ec2 import-key-pair \
  --key-name firesim-f2 \
  --public-key-material fileb://~/.ssh/id_ed25519.pub \
  --region us-east-1
```

### 1.2 创建安全组

```bash
SG_ID=$(aws ec2 create-security-group \
  --group-name firesim-f2-sg \
  --description "FireSim F2 SSH access" \
  --region us-east-1 \
  --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress \
  --group-id $SG_ID \
  --protocol tcp --port 22 \
  --cidr <YOUR_IP>/32 \
  --region us-east-1
```

### 1.3 创建 IAM Role + Instance Profile

```bash
# 创建信任策略
cat > /tmp/trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name FireSim-F2-Role \
  --assume-role-policy-document file:///tmp/trust-policy.json

aws iam attach-role-policy \
  --role-name FireSim-F2-Role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

aws iam create-instance-profile \
  --instance-profile-name FireSim-F2-Profile

aws iam add-role-to-instance-profile \
  --instance-profile-name FireSim-F2-Profile \
  --role-name FireSim-F2-Role
```

### 1.4 启动 F2 实例

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id ami-0b655f161063b404a \
  --instance-type f2.6xlarge \
  --key-name firesim-f2 \
  --security-group-ids $SG_ID \
  --subnet-id <us-east-1b-subnet-id> \
  --iam-instance-profile Name=FireSim-F2-Profile \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":200,"VolumeType":"gp3"}}]' \
  --region us-east-1 \
  --query 'Instances[0].InstanceId' --output text)

echo "Instance: $INSTANCE_ID"

# 等待 running 并获取 IP
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-east-1
F2_IP=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID --region us-east-1 \
  --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
echo "SSH: ssh -i ~/.ssh/id_ed25519 ubuntu@$F2_IP"
```

**实例规格参考**：

| 实例类型 | FPGA 数量 | 价格 | 适用 |
|----------|-----------|------|------|
| f2.6xlarge | 1 | $1.98/hr | 单核仿真、开发调试 |
| f2.12xlarge | 2 | $3.96/hr | 双核/多核设计 |
| f2.48xlarge | 8 | $15.84/hr | 集群仿真 |

---

## 第二阶段：环境搭建

### 2.1 SSH 连接

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$F2_IP
```

### 2.2 安装 Miniforge (Conda)

```bash
wget -q https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -O /tmp/miniforge.sh
bash /tmp/miniforge.sh -b -p $HOME/miniforge3
eval "$($HOME/miniforge3/bin/conda shell.bash hook)"
conda init bash
```

### 2.3 克隆 FireSim 并执行 build-setup

```bash
cd ~
git clone https://github.com/firesim/firesim.git
cd firesim

# 设置 Vivado 环境
source /opt/Xilinx/Vivado/2024.1/settings64.sh

# 初始化 FireSim（约 30-60 分钟，下载子模块 + 编译工具链）
./build-setup.sh
```

### 2.4 克隆 aws-fpga SDK（F2 分支）

```bash
cd ~
git clone --branch f2 https://github.com/aws/aws-fpga.git
cd aws-fpga
source sdk_setup.sh
```

### 2.5 激活 FireSim 环境

```bash
eval "$($HOME/miniforge3/bin/conda shell.bash hook)"
conda activate /home/ubuntu/firesim/.conda-env
cd ~/firesim
source sourceme-manager.sh
```

验证：
```bash
firesim --help   # 应显示帮助信息
sudo fpga-describe-local-image -S 0 -H   # 应显示 FPGA slot 状态
```

---

## 第三阶段：加载预构建 AGFI

### 3.1 可用的公共预构建 AGFI

| 配置名称 | AGFI | 描述 |
|----------|------|------|
| `firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3` | `agfi-0c5eedfce0e568971` | 四核 Rocket（顺序），4MB LLC，16GB DDR3 |
| `firesim_boom_singlecore_no_nic_l2_llc4mb_ddr3` | `agfi-0ec7ae76159d3bfb6` | 单核 BOOM（乱序），4MB LLC，16GB DDR3 |
| `firesim_megaboom_singlecore_no_nic_l2_llc4mb_ddr3` | `agfi-0f4582f3f39b9a8a4` | 单核 MegaBOOM（大配置乱序） |
| `firesim_rocket_quadcore_nic_l2_llc4mb_ddr3` | `agfi-013458e5c75f304a0` | 四核 Rocket + 网卡（集群仿真用） |

### 3.2 加载 AGFI 到 FPGA

```bash
# 加载 Rocket 四核
sudo fpga-load-local-image -S 0 -I agfi-0c5eedfce0e568971

# 或加载 BOOM 乱序核
# sudo fpga-load-local-image -S 0 -I agfi-0ec7ae76159d3bfb6

# 验证加载状态（应显示 "loaded" + "ok"）
sleep 5
sudo fpga-describe-local-image -S 0 -R -H
```

预期输出：
```
Type  FpgaImageSlot  FpgaImageId             StatusName    StatusCode   ErrorName    ErrorCode   ShVersion
AFI          0       agfi-0c5eedfce0e568971  loaded            0        ok               0       0x10212415
```

---

## 第四阶段：配置 FireSim Manager

### 4.1 HWDB 配置

```bash
cat > ~/firesim/deploy/config_hwdb.yaml << 'EOF'
firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3:
    agfi: agfi-0c5eedfce0e568971
    deploy_quintuplet_override: null
    custom_runtime_config: null

firesim_boom_singlecore_no_nic_l2_llc4mb_ddr3:
    agfi: agfi-0ec7ae76159d3bfb6
    deploy_quintuplet_override: null
    custom_runtime_config: null
EOF
```

### 4.2 运行时配置（本地单机模式）

```bash
cat > ~/firesim/deploy/config_runtime.yaml << 'EOF'
run_farm:
  base_recipe: run-farm-recipes/externally_provisioned.yaml
  recipe_arg_overrides:
    run_farm_tag: firesim-local
    run_farm_hosts_to_use:
      - localhost: one_fpgas_spec

metasimulation:
  metasimulation_enabled: false
  metasimulation_host_simulator: verilator
  metasimulation_only_plusargs: "+fesvr-step-size=128 +max-cycles=100000000"
  metasimulation_only_vcs_plusargs: "+vcs+initreg+0 +vcs+initmem+0"

target_config:
    topology: no_net_config
    no_net_num_nodes: 1
    link_latency: 6405
    switching_latency: 10
    net_bandwidth: 200
    profile_interval: -1
    default_hw_config: firesim_rocket_quadcore_no_nic_l2_llc4mb_ddr3
    plusarg_passthrough: ""

tracing:
    enable: no
    output_format: 0
    selector: 1
    start: 0
    end: -1

autocounter:
    read_rate: 0

workload:
    workload_name: linux-uniform.json
    terminate_on_completion: no
    suffix_tag: null

host_debug:
    zero_out_dram: no
    disable_synth_asserts: no

synth_print:
    start: 0
    end: -1
    cycle_prefix: yes
EOF
```

---

## 第五阶段：构建 Linux 镜像并运行仿真

### 5.1 构建 Linux 镜像（通过 Chipyard + FireMarshal）

```bash
# 克隆 Chipyard（如果尚未）
cd ~
git clone https://github.com/ucb-bar/chipyard
cd chipyard
git submodule update --init sims/firesim

# 构建 buildroot Linux（约 10-15 分钟）
cd software/firemarshal
./marshal -v build br-base.json
./marshal -v install br-base.json
```

生成文件：
- `images/firechip/br-base/br-base-bin` — bootloader + Linux kernel
- `images/firechip/br-base/br-base.img` — 根文件系统磁盘镜像

### 5.2 运行仿真

```bash
cd ~/firesim
source sourceme-manager.sh

# 基础设施设置（编译 driver、部署文件到本机）
firesim --platform f2 infrasetup

# 启动仿真（Linux 将在 RISC-V 上启动）
firesim --platform f2 boot

# 查看仿真输出
firesim --platform f2 runcheck
```

### 5.3 连接到仿真中的 Linux

```bash
# 通过 screen 连接 UART 输出
screen -r

# 或查看输出日志
cat /home/ubuntu/sim_slot_0/uartlog
```

---

## 第六阶段：性能数据采集

### 6.1 启用 AutoCounter（硬件性能计数器）

修改 `config_runtime.yaml`：
```yaml
autocounter:
    read_rate: 100000000   # 每 1 亿周期读一次计数器
```

采集的指标包括：
- IPC（Instructions Per Cycle）
- L1/L2 Cache miss rate
- TLB miss rate
- Branch mispredict rate

### 6.2 启用 TracerV（指令级 Trace）

```yaml
tracing:
    enable: yes
    output_format: 2    # 0=文本, 1=二进制, 2=FlameGraph
    selector: 1         # 周期触发
    start: 0
    end: -1             # -1 表示一直 trace
```

### 6.3 性能对比：Rocket vs BOOM

```bash
# 测试 1：Rocket 顺序核
sudo fpga-load-local-image -S 0 -I agfi-0c5eedfce0e568971
firesim --platform f2 boot
# → 记录 workload 执行时间和 IPC

# 测试 2：BOOM 乱序核
sudo fpga-load-local-image -S 0 -I agfi-0ec7ae76159d3bfb6
# 修改 config_runtime.yaml 中 default_hw_config
firesim --platform f2 boot
# → 对比相同 workload 的性能差异
```

---

## 成本与管理

### 费用估算

| 阶段 | 耗时 | 费用 |
|------|------|------|
| 环境搭建 | ~1 小时 | ~$2 |
| 使用预构建 AGFI 跑仿真 | 按需 | $1.98/hr |
| 自定义 AFI 编译（Vivado） | 4-14 小时 | $8-28 |
| Stop 后存储费 | 持续 | ~$0.53/天 (200GB gp3) |

### 实例管理

```bash
# 停止（保留数据，不收计算费）
aws ec2 stop-instances --instance-ids i-0876fcc35b0e86342 --region us-east-1

# 启动
aws ec2 start-instances --instance-ids i-0876fcc35b0e86342 --region us-east-1

# 获取新 IP（每次 start 后 IP 会变）
aws ec2 describe-instances --instance-ids i-0876fcc35b0e86342 --region us-east-1 \
  --query "Reservations[0].Instances[0].PublicIpAddress" --output text
```

---

## 已验证的环境信息

| 项目 | 值 |
|------|-----|
| AMI | ami-0b655f161063b404a (FPGA Developer AMI Ubuntu 22.04, v1.16.2) |
| Vivado 版本 | 2024.1 |
| 实例 ID | i-0876fcc35b0e86342 |
| 区域 / AZ | us-east-1 / us-east-1b |
| Key Pair | firesim-f2 |
| Security Group | sg-0b9462001ddc199b4 |
| IAM Profile | FireSim-F2-Profile |
| FPGA Shell 版本 | 0x10212415 |
| AGFI 加载验证 | ✅ agfi-0c5eedfce0e568971 (QuadRocket) loaded 成功 |

---

## 参考资源

- [FireSim 官方文档](https://docs.fires.im)
- [FireSim GitHub](https://github.com/firesim/firesim)
- [Chipyard](https://github.com/ucb-bar/chipyard)
- [BOOM 处理器](https://github.com/ucb-bar/riscv-boom)
- [AWS FPGA Developer AMI](https://aws.amazon.com/marketplace/pp?sku=e4txuxx6uz6371b7tgmotozac)
- [AWS F2 实例文档](https://docs.aws.amazon.com/ec2/latest/instancetypes/f2.html)
