# g7e PD 分离（Prefill/Decode Disaggregation）测试方案 + 一键脚本

> 目标：用最低成本验证 **g7e（RTX PRO 6000 96GB，PCIe Gen5，无 NVLink，EFAv4）能不能跑 PD 分离**，以及 KV 传输路径（同机 P2P / SHM、跨机 EFA + GDR）的真实开销。
> 状态：**方案 + 脚本，均未实测**。下文凡标「待验证」的都是推断或官方说法，以实测为准。
> 成本提示：g7e.48xlarge 按需约 **$33.14/h**（us-east-1，2026-11 Pricing API）；可用区 us-east-1a/c/d。**开实例前先确认。**

## TL;DR

| 阶段 | 机器 | 验证什么 | 通过标准 | 预估时长 / 成本 |
|---|---|---|---|---|
| **S0** 环境体检 | 1× g7e.48xl | 拓扑、P2P 分组、EFA/GDR 前置条件 | 拿到 P2P 分组矩阵、`nvidia_peermem`/EFA 状态 | 0.5h |
| **S1** 同机 1P1D | 1× g7e.48xl | vLLM + NixlConnector 能跑通，同机 KV 传输走哪条路 | 请求正确返回；TTFT/TPOT 有数据；日志里能看到 KV 传输后端 | 2–3h ≈ $100 |
| **S2** 跨机链路 | 2× g7e.48xl（cluster placement group） | EFA + GDR 是否真正生效 | NCCL 日志里出现 `NET/Libfabric/…/GDRDMA`；nixlbench VRAM→VRAM 带宽接近网卡上限 | 1–2h ≈ $130 |
| **S3** 跨机 1P1D | 同上 | NIXL 走 EFA 的端到端效果 | 跨机 TTFT 相对同机的增量 ≈ KV 大小 / 实测带宽 | 2h ≈ $130 |

**一句话结论模板**（测完填）：g7e 同机 1P1D ___，跨机 GDR ___，NIXL over EFA ___，建议 ___。

---

## 1. 背景：为什么 PD 分离要先测 KV 传输

- Prefill 是算力瓶颈，决定 TTFT；Decode 是显存带宽瓶颈，决定 TPOT。两者混跑时，长 prefill 会卡住 decode，造成 TPOT P99 抖动。
- PD 分离的代价是 **KV cache 要从 P 搬到 D**，搬运时间直接加到 TTFT 上：

| 模型 | 每 token KV | 4K prompt KV | @ ~45 GB/s（400G + GDR） | @ ~10 GB/s（SHM） | @ ~3 GB/s（无 GDR 跨机） |
|---|---|---|---|---|---|
| Llama-3.1-70B（GQA，bf16） | ≈ 320 KB | ≈ 1.3 GB | ≈ 29 ms | ≈ 130 ms | ≈ 440 ms |
| Qwen3-32B（GQA 8 KV head，64 层，bf16） | ≈ 256 KB | ≈ 1.0 GB | ≈ 23 ms | ≈ 100 ms | ≈ 350 ms |
| Qwen3-8B（36 层，8 KV head） | ≈ 144 KB | ≈ 0.6 GB | ≈ 13 ms | ≈ 60 ms | ≈ 200 ms |

> 每 token KV = 2 × 层数 × KV head 数 × head_dim × 字节数。上表为理论计算，带宽列为假设值。

所以测试顺序是：**先确认链路（P2P / GDR），再测 KV 微基准，最后跑端到端**。链路不通，端到端的数字没有意义。

## 2. g7e 已知信息与风险点

| 项 | 信息 | 来源 |
|---|---|---|
| g7e.48xlarge | 8× RTX PRO 6000 96GB，192 vCPU，2 TiB 内存，1600 Gbps，4 张网卡 / 4 个 EFA | `describe-instance-types`（实测查询） |
| g7e.24xlarge | 4 卡，800 Gbps，2 个 EFA | 同上 |
| 卡间互连 | PCIe Gen5，**无 NVLink** | 官方 |
| GPUDirect P2P | 官方称支持；**现场反馈：只有同一 PCIe switch 下的卡之间可以 P2P**，跨 switch 会退回 SHM | 官方 + 现场反馈（**待验证**） |
| GDR（EFA RDMA read/write） | EFA 文档标注 g7e 为 EFAv4，支持 RDMA r/w | 官方（**未实测**） |
| NIXL over EFA | NIXL 有 LIBFABRIC 后端；UCX 后端对 EFA SRD 的支持有限 | 社区 / 代码（**未实测，版本敏感**） |

**由此得到的设计约束**：
1. P 和 D 的 TP 组都放在 **同一个 P2P 组** 内（大概率是 TP=2，{0,1}、{2,3}…），否则 TP AllReduce 会走 SHM。
2. 同机 1P1D 要对比两种摆放：P 和 D 是否在同一 PCIe switch 下，这决定 KV 走 P2P 还是 SHM。
3. 跨机时，P 卡和 D 卡要与各自 NUMA 本地的 EFA 网卡对应。

## 3. 测试矩阵

### S0 环境体检（`00_env_check.sh`）
- `nvidia-smi topo -m`、`topo -p2p r`、PCIe 链路宽度
- `fi_info -p efa`、`lsmod | grep -E 'efa|nvidia_peermem'`、驱动/CUDA/NCCL/aws-ofi-nccl 版本
- 两两 GPU P2P 带宽矩阵（`p2pBandwidthLatencyTest` 或 nvbandwidth），**输出 P2P 分组**
- 产出：`results/env_<host>.txt`

### S1 同机 1P1D（`10_single_node_1p1d.sh` + `20_bench.sh`）

默认配置（可用环境变量覆盖）：

| 配置名 | Prefill | Decode | 说明 |
|---|---|---|---|
| `baseline` | — | — | 不分离：1 个 vLLM，`TP=2`，`DP=2`，占用 GPU 0–3（与 1P1D 用相同卡数） |
| `pd-same-switch` | GPU 0,1（TP=2） | GPU 2,3（TP=2） | 若 {0,1}{2,3} 在同一 switch 下，KV 可能走 P2P |
| `pd-cross-switch` | GPU 0,1（TP=2） | GPU 4,5（TP=2） | KV 预计走 SHM / 主机内存 |
| `pd-4x4` | GPU 0–3（TP=4） | GPU 4–7（TP=4） | 对应最初的「0–3 跑 P，4–7 跑 D」；TP=4 跨 switch，TP AllReduce 可能走 SHM，作为对照 |

> S0 测出真实的 P2P 分组后，再调整上面的 GPU 编号。

模型：`Qwen/Qwen3-8B` 冒烟测试 → `Qwen/Qwen3-32B` 正式测试（bf16 约 64 GB，TP=2 时每卡约 32 GB 权重，KV 空间充足）。

压测扫描：输入长度 `1024 / 4096 / 16384` × 输出长度 `256` × 请求速率 `1 / 2 / 4 / 8 / inf`。

指标：TTFT、TPOT、ITL、E2E 的 P50/P90/P99，吞吐，以及 SLO 下的 goodput（例：TTFT ≤ 2s 且 TPOT ≤ 50ms）。

### S2 跨机链路（`30_two_node_nccl.sh`、`31_nixlbench.sh`）
- nccl-tests `all_reduce_perf` 2 机 × 8 卡，`NCCL_DEBUG=INFO`，**grep `GDRDMA`**
- 对照：`FI_EFA_USE_DEVICE_RDMA` 不要手动设置；若日志里没有 GDRDMA，记录为「无 GDR」
- nixlbench：LIBFABRIC 后端，`VRAM→VRAM` 与 `DRAM→DRAM` 对比，块大小从 4 KB 扫到 64 MB（KV 是按 page / layer 分块传输的，**小块延迟更重要**）

### S3 跨机 1P1D（`32_two_node_1p1d.sh` + `20_bench.sh`）
- P 在 node A 的 GPU 0,1；D 在 node B 的 GPU 0,1；proxy 在 A
- NIXL 后端：先试 `UCX`（默认），再试 `LIBFABRIC`，记录哪个能跑通、带宽多少
- 与 S1 `pd-same-switch` 对比：ΔTTFT 应当 ≈ KV 大小 / 实测带宽

## 4. 判定标准

| 现象 | 结论 |
|---|---|
| S1 跑通，ΔTTFT（相对 baseline）< 10%，TPOT P99 明显下降 | 同机 PD 分离可用 |
| `pd-cross-switch` 的 TTFT 明显高于 `pd-same-switch` | KV 走 SHM，**P/D 摆放必须按 PCIe switch 对齐** |
| S2 日志有 `GDRDMA`，nixlbench VRAM 带宽 ≥ 每卡对应网卡带宽的 70% | g7e GDR 可用 |
| S2 只有 `NET/Libfabric`，没有 GDRDMA | 无 GDR，跨机 PD 分离只适合 MLA 等小 KV 模型或短 prompt |
| S3 UCX 跑不通、LIBFABRIC 能跑通 | NIXL over EFA 需要 LIBFABRIC 后端（记录版本） |

## 5. 脚本清单

| 脚本 | 在哪跑 | 作用 |
|---|---|---|
| `launch_g7e.sh` | 任意有 AWS CLI 的机器 | 创建 placement group + 启动 1 或 2 台 g7e.48xl（**默认 DRY_RUN=1，只打印命令**） |
| `01_install.sh` | 每台 g7e | 建 venv，安装 vLLM + NIXL + nccl-tests + vLLM 示例 proxy |
| `00_env_check.sh` | 每台 g7e | S0 体检，输出 P2P 分组 |
| `10_single_node_1p1d.sh` | g7e | S1：按 `MODE` 启动 baseline / 1P1D + proxy |
| `20_bench.sh` | 跑 proxy 的机器 | 扫描输入长度 × 请求速率，保存 json |
| `30_two_node_nccl.sh` | node A | S2：2 机 nccl-tests，检查 GDRDMA |
| `31_nixlbench.sh` | A、B 各一次 | S2：NIXL 传输微基准 |
| `32_two_node_1p1d.sh` | A、B 各一次 | S3：跨机 1P1D |
| `40_summarize.py` | 任意 | 汇总 bench json 为 Markdown 表 |
| `99_cleanup.sh` | g7e | 停掉 vLLM / proxy / etcd 进程 |

### 一键执行顺序
```bash
# 0) 本地：先 dry-run 看要执行的命令，确认后再真正开机
DRY_RUN=1 NODES=1 ./launch_g7e.sh
DRY_RUN=0 NODES=1 KEY_NAME=xxx SUBNET_ID=subnet-xxx SG_ID=sg-xxx ./launch_g7e.sh

# 1) g7e 上
./01_install.sh && source ~/pd-venv/bin/activate
./00_env_check.sh                                   # 看 P2P 分组，必要时调整 GPU 编号
MODEL=Qwen/Qwen3-8B MODE=pd-same-switch ./10_single_node_1p1d.sh
QUICK=1 ./20_bench.sh                                # 冒烟
for m in baseline pd-same-switch pd-cross-switch pd-4x4; do
  ./99_cleanup.sh; MODEL=Qwen/Qwen3-32B MODE=$m ./10_single_node_1p1d.sh && TAG=$m ./20_bench.sh
done
python3 40_summarize.py results/

# 2) 跨机（两台都执行 01/00）
NODE_B=<B私网IP> ./30_two_node_nccl.sh
ROLE=etcd ./31_nixlbench.sh   # A
ROLE=worker ETCD=<A私网IP> ./31_nixlbench.sh   # A 和 B 各一个
ROLE=decode ./32_two_node_1p1d.sh                         # B
ROLE=prefill NODE_B=<B私网IP> ./32_two_node_1p1d.sh       # A（同时起 proxy）
TAG=xnode-ucx ./20_bench.sh
```

## 6. 注意事项 / 已知坑

- **安全组**：同一安全组内放通全部流量（EFA 必须），22 端口按需开放。
- **Placement group**：跨机必须用 `cluster` 策略，同一可用区。
- `/dev/shm`：容器部署要 `--ipc=host`，否则 SHM 路径会报错或卡住。
- `VLLM_NIXL_SIDE_CHANNEL_PORT`：同机两个实例要用不同端口，脚本里已经区分。
- NIXL / vLLM 的接口变化很快：`kv_connector_extra_config.backends`、proxy 参数名、nixlbench 参数都可能随版本变化。**脚本固定了安装版本，测试报告里要写明版本。**
- 不要手动设置 `FI_EFA_USE_DEVICE_RDMA`，交给 aws-ofi-nccl 自动探测。
- 测试结束后立刻 `aws ec2 terminate-instances`。

## 7. 结果记录模板

| 配置 | 输入长度 | 速率 | TTFT P50/P99 (ms) | TPOT P50/P99 (ms) | 吞吐 (tok/s) | KV 传输路径 | 备注 |
|---|---|---|---|---|---|---|---|
| baseline | 4096 | 4 | | | | — | |
| pd-same-switch | 4096 | 4 | | | | P2P? | |
| pd-cross-switch | 4096 | 4 | | | | SHM? | |
| xnode-ucx | 4096 | 4 | | | | EFA? | |
| xnode-libfabric | 4096 | 4 | | | | EFA+GDR? | |

## 8. 待验证问题
1. g7e.48xl 的 P2P 分组到底是什么（2 卡 / 4 卡一组）？
2. g7e 跨机 NCCL 能否出现 GDRDMA？busbw 多少？
3. NIXL 的 UCX 后端能否在 EFA 上工作？LIBFABRIC 后端在 vLLM 里怎么启用、带宽多少？
4. 同机跨 switch 时 NIXL 走的是 cuda_ipc 还是经主机内存中转？
5. PD 分离在 g7e 上相对不分离的 goodput 收益拐点在哪个输入长度？

相关文档：[GPU 集群性能测试清单](../gpu-cluster-benchmark-checklist.md)（④ 跨机 EFA/GDR、⑥ 端到端推理）
