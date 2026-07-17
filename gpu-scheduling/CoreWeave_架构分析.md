# CoreWeave 架构分析

## 1. 核心定位

CoreWeave 是一家专注 GPU 计算的"GPU-first"云服务商，所有资源通过 Kubernetes 原生 API 管理，专为 AI/ML 训练和推理工作负载设计。

## 2. 基础设施架构

```
CoreWeave Cloud (Kubernetes-native)
├── Control Plane — K8s API Server
├── GPU Node Pools
│   ├── Training Pool (HGX H200 × N, InfiniBand 互联)
│   └── Inference Pool (按需扩缩)
├── 网络层
│   ├── InfiniBand Fabric (GPU-to-GPU RDMA)
│   └── Ethernet (管理 + 数据入口)
├── 存储层
│   ├── 高性能共享文件系统 (训练数据/checkpoint)
│   └── 对象存储
└── Kubernetes Services
    ├── Virtual Servers (KubeVirt)
    ├── Serverless Inference (KNative)
    └── Workflows (Argo)
```

## 3. GPU 调度架构

CoreWeave 的 GPU 调度完全由 Kubernetes 原生调度器完成，但做了深度定制：

```
Kubernetes Scheduler (定制)
├── GPU Resource Plugin (nvidia.com/gpu)
├── Topology-Aware Scheduling
│   ├── NVLink 拓扑感知 — 同 NVLink Domain 的 GPU 优先调度在一起
│   └── InfiniBand 拓扑感知 — 多节点训练优先选同一 IB Switch 下的节点
├── Priority & Preemption
│   └── 训练任务 vs 推理任务的优先级队列
└── Gang Scheduling
    └── 多节点分布式训练 All-or-Nothing 调度
```

### 关键组件

| 组件 | 作用 |
|------|------|
| NVIDIA GPU Operator | 管理 GPU 驱动、Device Plugin、监控 |
| K8s Device Plugin | 向 scheduler 暴露 `nvidia.com/gpu` 资源 |
| Topology-Aware Scheduling | 感知 NVLink/IB 拓扑，优化 GPU 分配 |
| NCCL Topology Config | 自动注入最优 NCCL 通信参数 |
| Kueue / 自研队列 | 多租户 GPU 配额管理和排队 |

### 用户提交训练任务示例

```yaml
apiVersion: batch/v1
kind: Job
spec:
  template:
    spec:
      containers:
      - name: training
        image: my-training:latest
        resources:
          requests:
            nvidia.com/gpu: 8
          limits:
            nvidia.com/gpu: 8
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: gpu.nvidia.com/class
                operator: In
                values: ["H100_SXM"]
```

## 4. 与 AWS HyperPod 对比

| 维度 | CoreWeave | AWS HyperPod |
|------|-----------|--------------|
| **编排** | 纯 Kubernetes | Slurm 或 EKS |
| **故障恢复** | 依赖 K8s 自愈 + 手动 | 自动节点替换 + checkpoint 恢复 |
| **网络** | InfiniBand 为主 | EFA (Elastic Fabric Adapter) |
| **GPU 健康检查** | 需自行实现 | 内置 Deep Health Checks |
| **生态** | K8s 原生，适合 K8s 用户 | 支持 Slurm（HPC 用户）+ EKS |
| **多租户** | namespace 隔离 | 账户/VPC 隔离 |
| **定价** | 按 GPU 时长，长期合约优惠大 | 按实例时长，RI/Savings Plans |
| **地域** | 美国为主，少量欧洲 | 全球 Region |
| **K8s 集成** | 原生 K8s（自建集群） | 仅支持 Amazon EKS，不支持自建 K8s |

### 调度对比

| 调度维度 | CoreWeave (K8s) | AWS HyperPod (Slurm) | AWS HyperPod (EKS) |
|---------|----------------|----------------------|---------------------|
| 调度器 | K8s Scheduler + 定制 | Slurm sched | K8s Scheduler |
| GPU 资源声明 | `nvidia.com/gpu` | `--gres=gpu:8` | `nvidia.com/gpu` |
| 拓扑感知 | NVLink + IB 拓扑 | Slurm topology plugin | EFA 拓扑 |
| Gang Scheduling | Volcano/自研 | Slurm 原生 | Volcano/Kueue |
| 多租户隔离 | Namespace + ResourceQuota | Slurm Partition + Account | Namespace |
| 故障恢复 | Pod Restart + 手动 | **自动节点替换** | **自动节点替换** |

## 5. CoreWeave 优势

- **K8s 原生**：对已有 K8s 工具链的团队几乎零适配成本
- **GPU 密度高**：单集群可提供数千张 GPU，InfiniBand 互联
- **定价**：大规模长期合约场景下价格有竞争力
- **GPU 调度深度定制**：拓扑感知 + Gang Scheduling，接近 Slurm 的 HPC 级调度效果

## 6. CoreWeave 劣势（AWS 竞争优势）

- **生态单一**：只有 GPU 计算，缺乏 S3/RDS/Lambda 等配套服务，数据处理/ETL/推理服务链不完整
- **地域有限**：主要在北美，无法满足全球部署和数据合规需求
- **故障恢复**：K8s 自愈 + 手动处理，不如 HyperPod 自动节点替换 + checkpoint 恢复
- **安全合规**：企业级合规能力弱于 AWS（缺少 IAM/CloudTrail/KMS 等完整体系）
- **不支持 Slurm**：对 HPC 传统用户不友好，只能用 K8s
- **GPU 健康检查**：缺乏内置 Deep Health Checks，大规模训练时 GPU 静默错误（silent data corruption）检测困难
- **锁定风险**：无通用云服务生态，一旦需要非 GPU 资源必须引入其他云商

## 7. HyperPod 关键差异化

1. **自动故障恢复**：节点故障时自动替换 + 从 checkpoint 恢复训练，无人值守
2. **Deep Health Checks**：内置 GPU/网络/存储健康检查，主动发现问题节点
3. **双编排支持**：Slurm（HPC 用户）+ EKS（K8s 用户）两种选择
4. **AWS 生态集成**：S3（数据湖）+ FSx Lustre（高性能存储）+ CloudWatch（监控）+ IAM（安全）一站式
5. **全球 Region**：满足数据驻留和延迟要求

## 8. 适合 CoreWeave 的场景

- 已有成熟 K8s 运维团队
- 纯 GPU 训练/推理，无需其他云服务
- 美国/欧洲单区域部署
- 长期大规模 GPU 合约（价格敏感）
- 对自动故障恢复要求不高（有人值守）

## 9. 适合 AWS HyperPod 的场景

- 需要全球多 Region 部署
- 大规模训练需要自动故障恢复（万卡训练不可能人工盯）
- 需要完整数据处理 Pipeline（S3 → SageMaker Processing → 训练 → 推理）
- 企业级安全合规要求（IAM/KMS/CloudTrail/VPC 隔离）
- 兼顾 HPC 用户（Slurm）和 K8s 用户（EKS）
