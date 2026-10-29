# g6e.24xlarge（4× L40S）NCCL AllReduce 实测与调优

> 测试时间：2026-10 | 工具：nccl-tests `all_reduce_perf` | 结论先行，原理与数据在后

## TL;DR

| 项 | 结论 |
|---|---|
| 卡间互联 | **无 NVLink、无 GPU P2P**（`topo -p2p r` 全 NS），NCCL 走 **SHM**（经主机内存中转） |
| PCIe 链路 | 每卡 **Gen4 x8**（GPU 能力 x16，`downgraded`）；两台不同实例一致 → **平台设计，非故障** |
| 默认带宽 | 4 卡 AllReduce 大消息 busbw ≈ **5.5 GB/s** |
| 调优后 | `NCCL_SHM_USE_CUDA_MEMCPY=1` → busbw ≈ **10.26 GB/s（×1.84）**，约 x8 链路实际可用带宽的 80%，基本到顶 |
| 不推荐 | `NCCL_SHM_MEMCPY_MODE=3`：所有尺寸都不如单开 memcpy，1 MB 处甚至比 baseline 慢 27% |
| 代价 | 小消息延迟 +5 µs（20.5 → 26.0 µs），≤64 KB 无收益 |
| 选型 | 适合单卡多副本 / LoRA / 出图 / Embedding / 少通信负载；**强 TP 推理、全参训练建议 NVLink 机型（p5/p5e/p6）** |

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

**busbw 与 algbw：** Ring AllReduce 中 `busbw = algbw × 2(n-1)/n`，4 卡 ×1.5、8 卡 ×1.75。busbw 用来对比链路利用率，algbw 才是单次操作的真实耗时依据。

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

## 7. 机型对比参考

| 机型 | GPU | 卡间互联 | 4/8 卡 AllReduce busbw | 备注 |
|---|---|---|---|---|
| g6e.24xlarge | 4× L40S 48 GB | PCIe Gen4 x8，无 P2P，SHM | 5.5 → **10.3 GB/s**（实测） | 本文 |
| g6e.48xlarge | 8× L40S（384 GB） | 同上（推断） | 预计相近；8 卡 algbw 更低（÷1.75） | 192 vCPU / 1.5 TB / 400 Gbps EFA，按需约 $30.13/h（us-east-1）；可能跨 NUMA，待实测 |
| g7e.24xlarge / 48xlarge | 4/8× RTX PRO 6000 96 GB | PCIe Gen5，无 NVLink，官方称支持 P2P | 待实测 | 800 / 1600 Gbps EFA |
| 8× RTX 5090 裸金属（参考） | GB202 32 GB | PCIe Gen5，无 NVLink | ≈ **40.5 GB/s**（torchrun 自测脚本，1 GiB） | 推断启用了 P2P；GeForce EULA 限制数据中心部署 |
| p5 / p5e / p6 | H100 / H200 / B200 | NVLink + NVSwitch | 数百 GB/s | 强 TP / 训练首选 |

**选型三问：**
1. **放不下？**（单卡显存不够）→ 需要多卡，优先 PP 而不是 TP
2. **算不完？**（吞吐不够）→ 多副本横向扩，卡间不通信，g6e 性价比高
3. **扛不住延迟？**（必须 TP 拆分且低延迟）→ 需要 NVLink 机型

---

## 8. 复现清单

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
- [ ] 5090 的 `topo -p2p r` 与 NCCL `via` 日志确认
- [ ] nvbandwidth H2D/D2H 实测
- [ ] 客户真实负载（vLLM TTFT/TPOT、训练 step time）开/关对比
