# g6e.24xlarge（4× L40S）NCCL AllReduce 实测与调优

> 测试时间：2026-10 | 工具：nccl-tests `all_reduce_perf` | 结论先行，原理与数据在后
> 相关：[GPU 集群性能测试清单（七层框架）](gpu-cluster-benchmark-checklist.md) — 本文属于其中第 ③ 层“单机卡间”

## TL;DR

| 项 | 结论 |
|---|---|
| 卡间互联 | **无 NVLink、无 GPU P2P**（`topo -p2p r` 全 NS），NCCL 走 **SHM**（经主机内存中转） |
| PCIe 链路 | 每卡 **Gen4 x8**（GPU 能力 x16，`downgraded`）；两台不同实例一致 → **平台设计，非故障** |
| 默认带宽 | 4 卡 AllReduce 大消息 busbw ≈ **5.5 GB/s** |
| 调优后 | `NCCL_SHM_USE_CUDA_MEMCPY=1` → busbw ≈ **10.26 GB/s（×1.84）**，约 x8 链路实际可用带宽的 80%，基本到顶 |
| 不推荐 | `NCCL_SHM_MEMCPY_MODE=3`：所有尺寸都不如单开 memcpy，1 MB 处甚至比 baseline 慢 27% |
| 代价 | 小消息延迟 +5 µs（20.5 → 26.0 µs），≤64 KB 无收益 |
| 跨实例 | g6e 支持 EFA 但**不支持 GPUDirect RDMA**（[aws-ofi-nccl#547](https://github.com/aws/aws-ofi-nccl/issues/547)），跨机经主机内存中转，社区实测约 3 GB/s；**g7e 多卡规格官方支持 GDR + P2P**，但 P2P 可能**仅限同一 PCIe switch 内**（见 7.2.1） |
| 选型 | 适合单卡多副本 / LoRA / 出图 / Embedding / 少通信负载；**强 TP 推理、全参训练建议 NVLink 机型（p5/p5e/p6）** |

---

## 0. 基础概念：AllReduce 是什么

> 给客户讲解时建议先讲这一节，再看后面的实测数据。

### 0.1 一句话

**多张卡各自算出一份数据后，把这些数据逐元素合并（通常是求和），再让每张卡都拿到同一份合并结果。**名字可以拆开理解：Reduce 是合并，All 是每张卡都拿到结果。

```
GPU0: [1, 2]
GPU1: [3, 4]      AllReduce(sum)      每张卡都得到:
GPU2: [5, 6]   ─────────────────▶     [16, 20]
GPU3: [7, 8]
```

类比：4 个店长各自统计本店的销售额，最后每个人手里都要有一份全公司的总账。

### 0.2 和其他集合通信操作的区别

| 操作 | 做什么 | 结果在哪 |
|---|---|---|
| Reduce | 合并 | 只在一张卡上 |
| **AllReduce** | 合并 | **每张卡都有** |
| Broadcast | 把一张卡的数据复制给所有卡 | 每张卡 |
| AllGather | 把各卡的数据拼接起来，不求和 | 每张卡 |
| ReduceScatter | 合并后切成 N 份，每张卡拿 1 份 | 分散在各卡 |

**AllReduce = ReduceScatter + AllGather**，NCCL 的 Ring 算法就是按这两步实现的。

### 0.3 在 AI 里用在哪

| 场景 | 为什么要 AllReduce |
|---|---|
| **数据并行训练（DP/DDP/FSDP）** | 每张卡处理不同的数据，算出各自的梯度。梯度求和后每张卡都拿到同一份，各卡用它更新参数，权重就能保持一致 |
| **张量并行推理（TP）** | 一层的矩阵被切到多张卡上计算，每张卡只得到部分结果，需要求和才是完整输出。**每一层都要做一次**，70B 模型生成一个 token 大约要做 160 次 |

所以 AllReduce 的性能会直接影响**训练的 step time** 和**推理的 TPOT**，这就是本文用 `all_reduce_perf` 衡量卡间互联的原因。

### 0.4 怎么看 nccl-tests 的输出：algbw 与 busbw

Ring 算法下，每张卡要发送和接收的数据量约为 **2(N−1)/N × 数据大小**：

| 指标 | 计算方式 | 含义 |
|---|---|---|
| **algbw**（算法带宽） | 数据大小 ÷ 耗时 | 业务实际感受到的速度，用来估算单次操作耗时 |
| **busbw**（总线带宽） | algbw × 2(N−1)/N | 链路实际承载的带宽，**用来和 PCIe/NVLink 理论值对比** |

- 系数：2 卡 ×1，4 卡 ×1.5，8 卡 ×1.75。卡数增加时，同样的链路下 busbw 相近，但 algbw 会下降。
- 例：本文 g6e 调优后 busbw 为 10.26 GB/s，对应 algbw ≈ 6.8 GB/s。
- 输出中的 `#wrong` 列必须为 0，表示结果正确。小消息看 `time`（µs，延迟），大消息看 `busbw`（带宽）。

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| 实例 | g6e.24xlarge（4× NVIDIA L40S 48 GB，96 vCPU，768 GiB，200 Gbps，EFA） |
| 主机 | ip-172-31-25-101（A/B 主测试机）、ip-172-31-24-14（交叉验证） |
| NCCL | 2.29.7+cuda13.2（25-101）/ 2.27.3（24-14） |
| GPU PCI 地址 | 63:00.0 / 65:00.0 / 67:00.0 / 69:00.0 |

### 构建 nccl-tests

```bash
git clone https://github.com/NVIDIA/nccl-tests.git && cd nccl-tests
make -j CUDA_HOME=/usr/local/cuda          # 需要 NCCL 头文件/库，pip 版用 NCCL_HOME 指过去
# 如果 NCCL 来自 venv 的 nvidia-nccl-cu*，运行前确保 LD_LIBRARY_PATH 正确，否则会直接退出、无任何输出
```

> ⚠️ 踩坑：新开终端没激活 venv / 没设 `LD_LIBRARY_PATH`，`all_reduce_perf` 启动即失败；若输出被 `grep` 过滤，会表现为"什么都没打印"。先不带 grep 跑一次看报错。

---

## 2. 拓扑诊断

### 2.1 `nvidia-smi topo -m`

4 张卡两两之间都是 **NODE**（同 NUMA 节点、跨 PCIe Host Bridge），NUMA 0，CPU 亲和 0-95。

### 2.2 `nvidia-smi topo -p2p r`

```
        GPU0  GPU1  GPU2  GPU3
GPU0    X     NS    NS    NS
GPU1    NS    X     NS    NS
GPU2    NS    NS    X     NS
GPU3    NS    NS    NS    X
```

`NS` = Not Supported。虚拟化 / ACS / IOMMU 层面禁用了 PCIe P2P，用户侧无法开启，`NCCL_P2P_LEVEL` 无效。

### 2.3 NCCL 日志

```bash
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,P2P,SHM ./build/all_reduce_perf -b 8 -e 8M -f 2 -g 4 2>&1 | grep -E "via|Channel"
```

关键行：`Channel 00 : 0[0] -> 1[1] via SHM/direct/direct`，2 个 Channel，Ring 0→1→2→3→0。

| 日志关键字 | 含义 |
|---|---|
| `P2P/CUMEM`、`P2P/IPC` | GPU 直连（PCIe P2P 或 NVLink） |
| `NVLS` | NVLink SHARP（NVSwitch 规约） |
| **`SHM/direct/direct`** | **经主机共享内存中转**（本机型） |
| `NET/IB`、`NET/Libfabric` | 跨机 RDMA（IB / EFA） |
| `NET/Socket` | 跨机 TCP 回退，通常说明 EFA 配置有误 |

### 2.4 PCIe 链路宽度

```bash
nvidia-smi --query-gpu=index,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current --format=csv
# 4 卡均为: gen max 4 / current 4, width max 16 / current 8

lspci -vv -s 63:00.0 | grep LnkSta
#   LnkSta: Speed 16GT/s, Width x8 (downgraded)
#   LnkSta2: ... EqualizationComplete+ EqualizationPhase1+
```

- `16GT/s` = Gen4；`x8` = 8 条 lane；`downgraded` = 低于 GPU 自身能力（LnkCap x16）
- `EqualizationComplete+` → 链路训练正常，**不是信号故障**
- `nvidia-smi -l 1` 持续采样始终 Gen4 x8 → **不是空闲节能降速**
- 两台不同实例（25-101、24-14）均为 x8 → **g6e.24xlarge 平台设计**

**带宽上限推算：**

| | 计算 | 值 |
|---|---|---|
| Gen4 每 lane | 16 GT/s × 128/130 ÷ 8 | ≈ 1.97 GB/s |
| x8 理论（单向） | × 8 | **≈ 15.75 GB/s** |
| 扣 TLP 开销后实际可用 | — | ≈ 12–13 GB/s |
| memcpy 实测 busbw | — | 10.26 GB/s（≈ 实际可用的 80%） |

> 补充验证（可选）：`nvbandwidth -t host_to_device_memcpy_ce -t device_to_host_memcpy_ce -t all_to_host_memcpy_ce`，x8 预期单卡 H2D ≈ 12–13 GB/s。

---

## 3. A/B 测试（同机 25-101、同 NCCL 版本）

### 3.1 命令

```bash
cd ~/nccl-tests
for cfg in "" "NCCL_SHM_USE_CUDA_MEMCPY=1" "NCCL_SHM_USE_CUDA_MEMCPY=1 NCCL_SHM_MEMCPY_MODE=3"; do
  echo "=== ${cfg:-baseline} ==="
  timeout 300 env $cfg ./build/all_reduce_perf -b 8 -e 1G -f 2 -g 4 -n 20 2>&1 \
    | grep -E "^ +(8|65536|1048576|67108864|1073741824) |Avg bus"
done
```

### 3.2 结果（out-of-place；#wrong 全部为 0）

| 消息大小 | baseline | **CUDA_MEMCPY=1** | MEMCPY=1 + MODE=3 |
|---|---|---|---|
| 8 B（纯延迟） | **20.52 µs** | 25.98 µs | 27.17 µs |
| 64 KB | 43.80 µs / 2.24 GB/s | 45.16 µs / 2.18 GB/s | 44.17 µs / 2.23 GB/s |
| 1 MB | 323.79 µs / 4.86 GB/s | **304.31 µs / 5.17 GB/s** | 410.59 µs / 3.83 GB/s ❌ |
| 64 MB | 18478.9 µs / 5.45 GB/s | **9835.58 µs / 10.23 GB/s** | 10474.1 µs / 9.61 GB/s |
| 1 GB | 288630 µs / 5.58 GB/s | **157034 µs / 10.26 GB/s** | 165562 µs / 9.73 GB/s |
| **Avg busbw** | 2.71 | **4.09** | 3.73 |

（in-place 数据与 out-of-place 差异 < 1%，略。）

### 3.3 完整曲线参考（24-14，NCCL 2.27.3，`CUDA_MEMCPY=1`）

| size | time (µs) | busbw (GB/s) |
|---|---|---|
| 8 KB | 26.38 | 0.47 |
| 64 KB | 40.85 | 2.41 |
| 128 KB | 71.68 | 2.74 |
| 256 KB | 185.36 | 2.12（协议切换凹点） |
| 512 KB | 225.66 | 3.49 |
| 1 MB | 302.04 | 5.21 |
| 4 MB | 775.27 | 8.12 |
| 16 MB | 2535.85 | 9.92 |
| 64 MB | 9831.71 | 10.24 |
| 1 GB | 157383 | 10.23 |

两台机器大消息结果一致（≈10.2 GB/s），可复现。

### 3.4 解读

1. **分界点在 64 KB ~ 1 MB**：以下 memcpy 无收益或略亏，以上收益随消息增大，≥16 MB 稳定在 ×1.84。
2. **小消息 +5 µs**：memcpy 模式由 CPU proxy 线程发起 `cudaMemcpyAsync` 交给 Copy Engine，有固定开销。
3. **MODE=3 更差**：收发两端都走 memcpy，开销叠加；且本轮完整跑完、未超时（早先一次 `exit=124` 与它无直接关系）。
4. **已接近物理上限**：10.26 GB/s ≈ x8 实际可用带宽 80%，再调 Channel/Buffer 预计 < 10%。

---

## 4. 原理：为什么是 SHM，memcpy 为什么更快

```
无 P2P（本机型）:
  GPU0 ──PCIe x8──▶ 主机内存(SHM buffer) ──PCIe x8──▶ GPU1

有 P2P:
  GPU0 ──PCIe──▶ (Switch/Root Complex) ──PCIe──▶ GPU1

NVLink:
  GPU0 ══NVLink/NVSwitch══▶ GPU1   （PCIe 仍在，负责 CPU/网卡/NVMe）
```

### 4.1 cudaMemcpy 是什么

`cudaMemcpy` / `cudaMemcpyAsync` 是 CUDA 的基础数据拷贝 API，用于 主机内存 ↔ 显存、显存 ↔ 显存 之间搬数据：

```c
cudaMemcpy(dst, src, size, cudaMemcpyHostToDevice);   // 主机 → 显存
cudaMemcpyAsync(dst, src, size, kind, stream);         // 异步版，NCCL 用这个
```

调用后由 GPU 上的 **Copy Engine（CE，拷贝引擎）** 执行 DMA。CE 是独立于 SM（计算核心）的专用搬运硬件。

### 4.2 `NCCL_SHM_USE_CUDA_MEMCPY` 的含义

无 P2P 时数据要经主机内存中转，问题是"谁把数据搬进/搬出主机内存"：

| | 默认（=0） | `=1` |
|---|---|---|
| 谁搬 | NCCL kernel 的 **SM 线程**直接读写主机 pinned 内存（zero-copy） | **CPU proxy 线程**调 `cudaMemcpyAsync`，由 **Copy Engine** DMA |
| 类比 | 厨师（计算核心）亲自跑仓库，一趟拿几件 | 叫物流车（DMA）整车拉 |
| 小消息 | ✅ 快，无启动开销 | ❌ 多一次"叫车"，约 +5 µs |
| 大消息 | ❌ SM 跨 PCIe 小粒度访问效率低，≈5.5 GB/s | ✅ 大块连续 DMA 接近链路上限，≈10.3 GB/s |
| SM 占用 | 占用 | 基本不占，可与计算重叠 |

对应实测：8 B 20.5 → 26.0 µs（叫车开销）；1 GB 5.58 → 10.26 GB/s（整车效率）。即 **大消息用它，小消息不划算**。

> 该参数只在 NCCL 走 **SHM** 时生效；有 P2P / NVLink 的机器数据 GPU 直传、不经主机内存，设了也无影响。

### 4.3 参数速查

| `NCCL_SHM_USE_CUDA_MEMCPY` | 数据搬运方式 | 特点 |
|---|---|---|
| 0（默认） | GPU SM 线程 zero-copy 直接读写主机 pinned 内存 | 延迟低；但 SM 发起的跨 PCIe 小粒度访问效率差，带宽低 |
| 1 | CPU proxy 调 `cudaMemcpyAsync`，**Copy Engine DMA** | 大块 DMA 吃满链路；有固定启动开销 |

| `NCCL_SHM_MEMCPY_MODE` | 含义 |
|---|---|
| 1（默认） | 发送端用 memcpy |
| 2 | 接收端用 memcpy |
| 3 | 两端都用 memcpy（每次"叫两趟车"，固定开销翻倍，本机型实测更差） |

> 不要设 `NCCL_SHM_DISABLE=1`：无 P2P 时会退化到 `NET/Socket`，更慢。

**busbw 与 algbw：** Ring AllReduce 中 `busbw = algbw × 2(n-1)/n`，4 卡 ×1.5、8 卡 ×1.75。busbw 用来对比链路利用率，algbw 才是单次操作的真实耗时依据（详见 0.4）。

---

## 5. 对业务负载的影响（估算）

以 70B（hidden 8192，bf16，TP=4，约 160 次 AllReduce/token）为例：

| 场景 | 单次 AllReduce 消息 | 建议 |
|---|---|---|
| 训练 / FSDP 梯度同步 | 数十 MB ~ GB | ✅ 开，通信时间约减半（7B 梯度 14 GB：~4 s → ~2 s/step） |
| Prefill（2K tokens） | ≈ 32 MB | ✅ 开，约 6 ms → 3.3 ms / 次 |
| Decode batch ≥ 64 | ≥ 1 MB | ✅ 开，小幅收益 |
| Decode batch 1–8 | 16–128 KB | ⚠️ 持平或略亏，约 +0.5 ms/token，以 TTFT/TPOT 实测为准 |
| 单卡多副本 / LoRA / 出图 / Embedding | 无卡间通信 | 无影响 |

补充：
- vLLM 的 custom allreduce 依赖 P2P，在本机型自动禁用，全部走 NCCL，所以此参数对 vLLM 同样有效。
- 混合负载一般整体开启更划算（prefill 节省 > decode 损失）。

---

## 6. 客户侧如何配置

NCCL 只读环境变量，**无需改代码**；需在 NCCL 初始化（`init_process_group`）之前设置，所有 rank 一致。

### 6.1 全局配置（推荐，零侵入）

```bash
echo "NCCL_SHM_USE_CUDA_MEMCPY=1" | sudo tee -a /etc/nccl.conf   # 或 ~/.nccl.conf
```

可直接打进 AMI / 容器镜像；同名环境变量优先级更高。

### 6.2 各框架

| 场景 | 写法 |
|---|---|
| torchrun / deepspeed / accelerate | 启动前 `export NCCL_SHM_USE_CUDA_MEMCPY=1` |
| Python 内 | `os.environ["NCCL_SHM_USE_CUDA_MEMCPY"]="1"`，**必须在 `init_process_group` 之前** |
| vLLM | `NCCL_SHM_USE_CUDA_MEMCPY=1 vllm serve <model> -tp 4` |
| vLLM + Ray | 每个节点 `ray start` 前 export，或 `runtime_env={"env_vars":{...}}` |
| SGLang | `NCCL_SHM_USE_CUDA_MEMCPY=1 python -m sglang.launch_server ... --tp 4` |
| Docker | `docker run -e NCCL_SHM_USE_CUDA_MEMCPY=1 --ipc=host ...`（或 `--shm-size=16g`） |
| Kubernetes | Pod `env:` 中添加；`/dev/shm` 挂 `emptyDir: {medium: Memory}` |
| Slurm | sbatch 中 export（srun 默认继承） |
| SageMaker Training | Estimator `environment={"NCCL_SHM_USE_CUDA_MEMCPY": "1"}` |

> ⚠️ 容器默认 `/dev/shm` 只有 64 MB，而本机型 NCCL 依赖 SHM 传输，过小会报错或 hang。

### 6.3 验证是否生效

```bash
NCCL_DEBUG=INFO <启动命令> 2>&1 | grep -E "NCCL_SHM_USE_CUDA_MEMCPY|via SHM"
# 期望：NCCL_SHM_USE_CUDA_MEMCPY set by environment to 1
#       ... via SHM/direct/direct
```

验证后去掉 `NCCL_DEBUG=INFO`。

---

## 7. 跨实例通信：EFA 与 GPUDirect RDMA

> 前面 1–6 节讲**单机内**卡间通信（PCIe / P2P / SHM）。本节讲**多台实例之间**的通信（EFA 网卡），是另一条链路，但根因相似：**g6e 的 GPU 不能直接和外设 DMA**。

### 7.1 g6e：支持 EFA，但不支持 GPUDirect RDMA

官方佐证：[aws/aws-ofi-nccl#547](https://github.com/aws/aws-ofi-nccl/issues/547)（AWS 维护者 bwbarrett 回复）：

| 要点 | 原文含义 |
|---|---|
| L40S 芯片本身支持 GDR | 但 **G6e 平台硬件不支持**，因为 G6e 定位单机推理 |
| 支持 GDR 的机型 | 截至 2025-08：**仅 p4d 及之后的 P 系列**（G7e 于 2026-01 发布，见 7.2） |
| 没有 GDR 能不能跑 | 能。NCCL 仍正常工作，只是路径变为 **GPU → 主机内存 → EFA 网卡 → 对端主机内存 → GPU** |
| `FI_EFA_USE_DEVICE_RDMA` | **不要手动设置**。在不支持的平台上强开会直接 abort（如 g4dn.8xlarge） |
| 提问者实测 | g6e 多机 nccl-tests 最高约 **3 GB/s** busbw |

**和单机 SHM 的关系**：

```
单机  GPU0 ──PCIe──▶ 主机内存(SHM) ──PCIe──▶ GPU1          （无 P2P）
跨机  GPU0 ──PCIe──▶ 主机内存 ──▶ EFA ══网络══▶ EFA ──▶ 主机内存 ──PCIe──▶ GPU1   （无 GDR）
```

两者都多了一次"经主机内存中转"，CPU 内存带宽和 PCIe x8 链路成为瓶颈。区别是：单机可以用 `NCCL_SHM_USE_CUDA_MEMCPY=1` 把搬运交给 Copy Engine 来提速；跨机没有对应的用户侧开关可以绕过。

### 7.2 g7e：官方支持 GPUDirect P2P + GPUDirect RDMA（EFA）

来源：[AWS News Blog — Announcing Amazon EC2 G7e instances](https://aws.amazon.com/blogs/aws/announcing-amazon-ec2-g7e-instances-accelerated-by-nvidia-rtx-pro-6000-blackwell-server-edition-gpus/)（2026-01-20）

| 能力 | 官方表述 | 对比 g6e |
|---|---|---|
| GPU | RTX PRO 6000 Blackwell Server Edition，96 GB/卡 | 显存 ×2，显存带宽 ×1.85 |
| 单机卡间 | **支持 GPUDirect P2P**（走 PCIe）；同一 PCIe switch 下的 GPU 之间 P2P 延迟最低 | 卡间带宽 **最高 ×4**（g6e 无 P2P，只能 SHM） |
| 跨机网络 | 最高 1600 Gbps；**多卡规格支持 GPUDirect RDMA over EFA** | 网络带宽 ×4，并且 g6e 不支持 GDR |
| 存储 | 多卡规格支持 GPUDirect Storage + FSx for Lustre，最高 1.2 Tbps | 加快模型加载 |
| 推理性能 | 最高为 G6e 的 2.3 倍 | — |
| 上线区域 | us-east-1、us-east-2（发布时） | — |

规格（官方博客，网络/EFA 已用 `aws ec2 describe-instance-types` 核对）：

| 规格 | GPU | 显存 | vCPU | 内存 | 网络 | EFA 接口 | 对应 g6e 网络 |
|---|---|---|---|---|---|---|---|
| g7e.12xlarge | 2 | 192 GB | 48 | 512 GiB | 400 Gbps | 1 | g6e.12xlarge 100 Gbps |
| g7e.24xlarge | 4 | 384 GB | 96 | 1024 GiB | 800 Gbps | 2 | g6e.24xlarge 200 Gbps |
| g7e.48xlarge | 8 | 768 GB | 192 | 2048 GiB | 1600 Gbps | 4 | g6e.48xlarge 400 Gbps |

> **推断（待实测）**：g6e 单机 memcpy 后约 10 GB/s，按官方"最高 ×4"估算，**同一 PCIe switch 内**的卡对 busbw 可能在 **~40 GB/s** 量级，和 8× RTX 5090 裸金属实测 40.5 GB/s 接近（两者都是 Gen5 + P2P、无 NVLink）。**跨 switch 的情况见 7.2.1**，4/8 卡整体数值大概率达不到这个量级。

#### 7.2.1 ⚠️ g7e 的 P2P 可能只在同一 PCIe switch 内生效

**信息来源与可信度**

| 类型 | 内容 |
|---|---|
| 官方原话（博客） | *"These instances offer the lowest peer to peer latency for GPUs **on the same PCIe switch**."* |
| 官方原话（产品页） | *"supports NVIDIA GPUDirect P2P via PCIe"*，没有说明是否所有卡对都支持 |
| 一线反馈（未见于官方文档） | 只有同一 PCIe switch 下的 GPU 能 P2P direct；**跨 switch 的 P2P 被平台禁用** |
| 状态 | **推断，待上机确认**（`topo -p2p r` + NCCL `via` 日志） |

**原理：为什么同 switch 能直连，跨 switch 不行**

```
              CPU Root Complex / IOMMU        ← 跨 switch 的 P2P 必须上行到这里
               /                    \
        PCIe Switch A            PCIe Switch B
         /    |    \              /    |    \
      GPU0  GPU1  EFA0         GPU2  GPU3  EFA1
       └─ P2P ─┘                  └─ P2P ─┘     switch 内直接转发 TLP，不经过 CPU
            GPU1 ↔ GPU2：要经过 Root Complex → 平台禁用 → NCCL 退回 SHM
```

| 路径 | 数据怎么走 | 云上为什么容易被关 |
|---|---|---|
| 同一 switch（`PIX`） | switch 内直接转发 | 不经过 IOMMU，隔离风险可控，性能最好 |
| 跨 switch、同一 CPU（`NODE`） | 上行到 Root Complex，经 IOMMU 转换后再下行 | ① 为了隔离 VM，ACS 会把 P2P 流量重定向到 IOMMU 检查；② 服务器 CPU 的 Root Complex 转发 P2P 时，性能和稳定性经常不如走主机内存；③ NVIDIA 驱动把这条路径列为需要平台单独确认 |
| 跨 CPU（`SYS`，48xl 双路） | 还要再经过 UPI 等 socket 间互联 | 基本不可用 |

结论：和 g6e 一样，这是**平台层（Nitro/hypervisor）的策略，用户侧打不开**。`NCCL_P2P_LEVEL=SYS`、`NCCL_P2P_DISABLE=0` 都没用。区别在于 g6e 全部卡对都关了，g7e 只放开了 switch 内部。

**推测拓扑（待实测）**：EFA 和 NVMe 的数量与 switch 数量对得上（24xl：2 EFA、2 NVMe；48xl：4 EFA、4 NVMe），所以很可能**每个 switch 下挂 2 张 GPU + 1 块 EFA + 1 块 NVMe**：

| 规格 | 推测 switch 数 | P2P 可直连的组 |
|---|---|---|
| g7e.12xlarge（2 卡） | 1 | {0,1} 全部直连 |
| g7e.24xlarge（4 卡） | 2 | {0,1}、{2,3} |
| g7e.48xlarge（8 卡） | 4（可能分布在 2 个 CPU 上） | {0,1}、{2,3}、{4,5}、{6,7} |

**对负载的影响**

| 场景 | 预期 |
|---|---|
| TP=2，两张卡在同一 switch | 全程 P2P，最优 |
| TP=4 / TP=8 | 环路中有一段走 SHM。Ring AllReduce 的带宽由最慢的一段决定，**整体会被 SHM 那段拉低**。NCCL 日志中会同时出现 `via P2P/CUMEM` 和 `via SHM` |
| vLLM custom allreduce | 要求所有 TP rank 两两都能 P2P。跨 switch 时自动关闭，退回 NCCL |
| 官方"卡间带宽 4 倍" | 很可能指 switch 内的 P2P，或者 CPU↔GPU 带宽，**不能当作 TP=8 的整体带宽** |
| 跨 switch 的 SHM 段 | 比 g6e 快。官方称 CPU↔GPU 带宽为 g6e 的 4 倍，正好等于 Gen5 x16 与 Gen4 x8 的比值。`NCCL_SHM_USE_CUDA_MEMCPY=1` 在这一段仍然有效，需要做 A/B |

**g7e 选型建议（推断，实测后修订）**

- 首选 **TP=2 × 多副本**（48xl 跑 4 个副本，24xl 跑 2 个），或者 **TP=2 + PP**（同一 switch 内做 TP，跨 switch 做 PP，PP 的通信量小）。
- 用 `CUDA_VISIBLE_DEVICES` 把同一 switch 下的卡分到一组（先用 `topo -m` 确认哪两张是 `PIX`）。K8s 上要开 Topology Manager，或使用 NVIDIA device plugin 的拓扑感知分配。
- 必须用 TP=4/8 时（单卡 96 GB 放不下，又不想用 PP），先用客户自己的负载实测 TPOT。
- 卡间强耦合（TP=8 低延迟、全参训练）仍然要选 NVLink 机型。

**上机验证清单**

```bash
nvidia-smi topo -m          # 同 switch 显示 PIX；跨 switch 显示 NODE；跨 CPU 显示 SYS
nvidia-smi topo -p2p r      # 预期：只有 PIX 的卡对是 OK，其余为 NS
nvidia-smi --query-gpu=index,pci.bus_id,pcie.link.gen.current,pcie.link.width.current --format=csv
lspci -tv | grep -B3 -A3 -i nvidia     # 看 GPU 和 EFA 挂在哪个 switch 下

# 传输路径：预期 P2P 和 SHM 同时出现
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,P2P,SHM ./build/all_reduce_perf -b 8 -e 8M -f 2 -g 8 2>&1 | grep via

# 同 switch 与跨 switch 对比（按 topo -m 结果替换卡号）
CUDA_VISIBLE_DEVICES=0,1 ./build/all_reduce_perf -b 1M -e 1G -f 2 -g 2   # PIX
CUDA_VISIBLE_DEVICES=0,2 ./build/all_reduce_perf -b 1M -e 1G -f 2 -g 2   # NODE
CUDA_VISIBLE_DEVICES=0,4 ./build/all_reduce_perf -b 1M -e 1G -f 2 -g 2   # SYS（48xl）

# 跨 switch 段的 SHM 调优 A/B（8 卡整体）
for cfg in "" "NCCL_SHM_USE_CUDA_MEMCPY=1"; do
  echo "=== ${cfg:-baseline} ==="
  timeout 300 env $cfg ./build/all_reduce_perf -b 8 -e 1G -f 2 -g 8 -n 20 2>&1 | grep -E "Avg bus"
done

# 参考：nvbandwidth 卡对矩阵，直接看出哪些卡对是直连
./nvbandwidth -t device_to_device_memcpy_read_ce
```

### 7.3 怎么验证有没有走 GDR

```bash
# 1) EFA 设备是否可见
fi_info -p efa -t FI_EP_RDM | head

# 2) NCCL 选中的网络插件与是否 GDR
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET <你的多机命令> 2>&1 | grep -E "NET/OFI|NET/Libfabric|Using network|GDRDMA|GDR"
#   有 GDR：通道日志里出现  via NET/Libfabric/0/GDRDMA
#   无 GDR：只有          via NET/Libfabric/0        （g6e 上就是这种）
#   退化到 TCP：           Using network Socket       （说明 aws-ofi-nccl 插件没加载，需检查 LD_LIBRARY_PATH / DLAMI 环境）

# 3) 多机 nccl-tests（2 台 × 4 卡示例）
mpirun -np 8 -N 4 --hostfile hosts \
  -x LD_LIBRARY_PATH -x FI_PROVIDER=efa -x NCCL_DEBUG=INFO \
  --mca pml ^cm --mca btl tcp,self --bind-to none \
  ./build/all_reduce_perf -b 8 -e 4G -f 2 -g 1
```

注意：
- 不要设置 `FI_EFA_USE_DEVICE_RDMA=1`，新版本 aws-ofi-nccl 会自动探测。
- 多机实例要放在同一个 **Cluster Placement Group**，安全组需要放行组内所有流量（EFA 要求 self-referencing 规则）。
- 每块 EFA 网卡都要挂载（g6e.24x / g7e.24x 各 2 个，48x 各 4 个），否则带宽只有一部分。

### 7.4 跨机场景选型口径

| 场景 | g6e | g7e | P 系列（p5/p5e/p6） |
|---|---|---|---|
| 多副本推理，副本间不通信 | ✅ 性价比高 | ✅ | 浪费 |
| 单机 TP（≤4/8 卡） | ⚠️ 只有 SHM，约 10 GB/s | ✅ TP=2（同一 switch 内 P2P）；⚠️ TP=4/8 跨 switch 退回 SHM（见 7.2.1） | ✅✅ NVLink |
| 跨机 PP / 小规模 DP | ⚠️ 能跑，无 GDR，约 3 GB/s（社区实测） | ✅ GDR + 最高 1600G，适合"小规模多机"（官方表述） | ✅✅ |
| 跨机 TP / 大规模训练 | ❌ | ⚠️ 规模有限，需实测 | ✅✅ 首选（3200G EFA + GDR + NVLink） |

---

## 8. 机型对比参考

| 机型 | GPU | 卡间互联 | 4/8 卡 AllReduce busbw | 跨机 GDR | 备注 |
|---|---|---|---|---|---|
| g6e.24xlarge | 4× L40S 48 GB | PCIe Gen4 x8，无 P2P，SHM | 5.5 → **10.3 GB/s**（实测） | ❌（[#547](https://github.com/aws/aws-ofi-nccl/issues/547)） | 本文；200 Gbps / 2 EFA |
| g6e.48xlarge | 8× L40S（384 GB） | 同上（推断） | 预计相近；8 卡 algbw 更低（÷1.75） | ❌ | 192 vCPU / 1.5 TB / 400 Gbps EFA，按需约 $30.13/h（us-east-1）；可能跨 NUMA，待实测 |
| g7e.24xlarge / 48xlarge | 4/8× RTX PRO 6000 96 GB | PCIe Gen5，无 NVLink，**GPUDirect P2P 可能仅限同一 PCIe switch 内**（跨 switch 退回 SHM，待实测） | 待实测；官方称卡间带宽最高为 g6e 的 4 倍（大概率指 switch 内） | ✅（官方：多卡规格支持） | 800 / 1600 Gbps EFA，支持 GDS |
| 8× RTX 5090 裸金属（参考） | GB202 32 GB | PCIe Gen5，无 NVLink | ≈ **40.5 GB/s**（torchrun 自测脚本，1 GiB） | — | 推断启用了 P2P；GeForce EULA 限制数据中心部署 |
| p5 / p5e / p6 | H100 / H200 / B200 | NVLink + NVSwitch | 数百 GB/s | ✅（p4d 起） | 强 TP / 训练首选，3200 Gbps EFA |

**选型三问：**
1. **放不下？**（单卡显存不够）→ 需要多卡，优先 PP 而不是 TP
2. **算不完？**（吞吐不够）→ 多副本横向扩，卡间不通信，g6e 性价比高
3. **扛不住延迟？**（必须 TP 拆分且低延迟）→ 需要 NVLink 机型

---

## 9. 复现清单

```bash
# 拓扑
nvidia-smi topo -m
nvidia-smi topo -p2p r
nvidia-smi --query-gpu=index,pcie.link.gen.current,pcie.link.width.current --format=csv
for b in 63 65 67 69; do echo "== $b:00.0"; lspci -vv -s $b:00.0 | grep -E "LnkCap:|LnkSta:"; done

# 传输路径
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,P2P,SHM ./build/all_reduce_perf -b 8 -e 8M -f 2 -g 4 2>&1 | grep via

# A/B
for cfg in "" "NCCL_SHM_USE_CUDA_MEMCPY=1"; do
  echo "=== ${cfg:-baseline} ==="
  timeout 300 env $cfg ./build/all_reduce_perf -b 8 -e 1G -f 2 -g 4 -n 20 2>&1 | grep -E "Avg bus"
done
```

## 待办 / 未验证

- [ ] g6e.48xlarge 实测（NUMA 分布、P2P、8 卡 busbw）
- [ ] g7e P2P 与 PCIe 宽度实测，和 5090 用 nccl-tests 同口径对比
- [ ] g7e 确认 P2P 是否仅限同一 PCIe switch 内（`topo -p2p r`，PIX/NODE/SYS 卡对分别测带宽）
- [ ] g7e TP=4/8 下 `NCCL_SHM_USE_CUDA_MEMCPY=1` 的 A/B
- [ ] 5090 的 `topo -p2p r` 与 NCCL `via` 日志确认
- [ ] nvbandwidth H2D/D2H 实测
- [ ] g6e 多机 nccl-tests 实测（验证约 3 GB/s 的社区数据）
- [ ] g7e 多机 GDR 实测（日志中确认 `GDRDMA`）
- [ ] 客户真实负载（vLLM TTFT/TPOT、训练 step time）开/关对比
