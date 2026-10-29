# GPU 集群性能测试清单（七层框架 + 可直接运行的命令）

> 适用：AWS GPU 实例（g6e / g7e / p4d / p5 / p6 等）及裸金属 GPU 服务器的验收、选型对比、故障排查。
> 标注约定：**【官方】** 厂商规格/文档原话；**【实测】** 我们自己跑出来的数据；**【推断】** 经验估算，需上机确认。
> 相关文档：[g6e.24xlarge NCCL AllReduce 实测与调优](g6e-nccl-allreduce-benchmark.md)

---

## TL;DR

GPU 集群的性能取决于**最慢的那一层**。从下往上按七层逐层测，每层只回答一个问题：

| 层 | 回答的问题 | 核心工具 | 关键指标 |
|---|---|---|---|
| ① 单卡 | 卡本身达标吗？ | PyTorch matmul、`nvidia-smi dmon`、gpu-burn | TFLOPS、显存带宽、频率/功耗是否降 |
| ② 主机 ↔ GPU | 喂数据的通道够宽吗？ | `nvbandwidth`、`lspci`、`nvidia-smi topo` | H2D/D2H GB/s、PCIe Gen/宽度、NUMA 亲和 |
| ③ 单机卡间 | 卡和卡之间多快？ | `nvidia-smi topo -m`、`p2pBandwidthLatencyTest`、nccl-tests | P2P 是否可用、busbw、小消息延迟 |
| ④ 跨机网络 | 机器和机器之间多快？ | `fi_info`、`fi_pingpong`、nccl-tests + MPI | EFA 是否生效、GDR 是否生效、跨机 busbw |
| ⑤ 存储 | 数据/模型/checkpoint 读写够快吗？ | `fio`、`gdsio`、`s5cmd` | 顺序读 GB/s、IOPS、模型加载时长 |
| ⑥ 端到端业务 | 业务实际跑多快？ | `vllm bench serve`、训练 tokens/s | TTFT、TPOT、吞吐、MFU |
| ⑦ 稳定性与健康 | 能长时间稳定跑吗？ | `dcgmi diag`、ECC/XID、长时间 burn | 无 XID、无 ECC 错误、无降频 |

**测试顺序建议**：⑦（先做健康检查，排除坏卡）→ ① → ② → ③ → ④ → ⑤ → ⑥。前几层不达标时，⑥ 的数据没有意义。

---

## 0. 准备：环境信息采集（每台机器先跑一遍）

先把环境信息存档，否则之后结果无法对比。

```bash
mkdir -p ~/bench && cd ~/bench
{
  echo "== host ==";      hostname; date -u
  echo "== instance ==";  TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60") && \
                          curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type; echo
  echo "== os/kernel ==";  cat /etc/os-release | head -2; uname -r
  echo "== cpu/numa ==";  lscpu | egrep 'Model name|Socket|NUMA'
  echo "== driver ==";    nvidia-smi --query-gpu=index,name,driver_version,memory.total,pcie.link.gen.max,pcie.link.width.max --format=csv
  echo "== cuda ==";      nvcc --version 2>/dev/null | tail -1
  echo "== torch/nccl =="; python -c "import torch;print(torch.__version__, torch.cuda.nccl.version())" 2>/dev/null
  echo "== efa ==";       fi_info -p efa 2>/dev/null | grep -c provider || echo "no efa"
  echo "== topo ==";      nvidia-smi topo -m
} 2>&1 | tee env_$(hostname).txt
```

---

## ① 单卡：算力 / 显存带宽 / 功耗与频率

### 1.1 算力（矩阵乘 TFLOPS）

```bash
cat > ~/bench/matmul.py <<'EOF'
import torch, time, sys
dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32, "tf32": torch.float32}[sys.argv[1] if len(sys.argv) > 1 else "bf16"]
if len(sys.argv) > 1 and sys.argv[1] == "tf32":
    torch.backends.cuda.matmul.allow_tf32 = True
N = 8192
for dev in range(torch.cuda.device_count()):
    torch.cuda.set_device(dev)
    a = torch.randn(N, N, device="cuda", dtype=dtype); b = torch.randn(N, N, device="cuda", dtype=dtype)
    for _ in range(10): a @ b
    torch.cuda.synchronize(); it = 50; t = time.time()
    for _ in range(it): a @ b
    torch.cuda.synchronize(); dt = time.time() - t
    print(f"GPU{dev} {torch.cuda.get_device_name(dev)} {sys.argv[1] if len(sys.argv)>1 else 'bf16'}: {2*N**3*it/dt/1e12:.1f} TFLOPS")
EOF
python ~/bench/matmul.py bf16
python ~/bench/matmul.py fp32
```

判断方法：
- 每张卡结果相差应该在 **±5% 以内**。某张卡明显偏低时，先看 1.3 的频率和功耗。
- 参考值：L40S 的 BF16 dense Tensor 峰值约 362 TFLOPS **【官方】**。dense matmul 实际能到峰值的 60%–75% **【推断】**。
- FP8 用 `torch._scaled_mm` 或 Transformer Engine 测。只有 Ada / Hopper / Blackwell 支持 FP8。

### 1.2 显存带宽

```bash
cat > ~/bench/membw.py <<'EOF'
import torch, time
n = 1 << 30   # 1 GiB 的 float32 元素数 → 4 GiB
for dev in range(torch.cuda.device_count()):
    torch.cuda.set_device(dev)
    x = torch.empty(n, device="cuda"); y = torch.empty_like(x)
    for _ in range(3): y.copy_(x)
    torch.cuda.synchronize(); it = 20; t = time.time()
    for _ in range(it): y.copy_(x)
    torch.cuda.synchronize(); dt = time.time() - t
    print(f"GPU{dev}: {2*x.numel()*4*it/dt/1e9:.0f} GB/s (read+write)")
EOF
python ~/bench/membw.py
```

也可以用 nvbandwidth（安装方法见 2.1）：`./nvbandwidth -t device_local_copy`

判断方法：
- 参考值：L40S 为 GDDR6，峰值 864 GB/s **【官方】**。copy 实测一般能到峰值的 75%–90% **【推断】**。
- HBM 卡（H100/H200/B200）带宽比 GDDR 卡高几倍。LLM decode 阶段受显存带宽限制，这个指标直接决定 TPOT。

### 1.3 功耗 / 频率 / 温度（压测时同时观察）

```bash
# 终端 A：压测（任选其一）
git clone https://github.com/wilicc/gpu-burn ~/gpu-burn && cd ~/gpu-burn && make && ./gpu_burn -tc -d 300   # -tc 使用 Tensor Core，-d 表示持续秒数
# 终端 B：每秒采样
nvidia-smi dmon -s pucvt -d 1 | tee ~/bench/dmon_$(hostname).log
# 压测后检查是否出现降频及原因
nvidia-smi -q -d PERFORMANCE,CLOCK,POWER | egrep -i 'throttle|slowdown|Reason|Power Draw|Graphics' | head -40
```

判断方法：
- 满载时功耗应该接近 TDP（L40S 为 350 W **【官方】**），SM 频率不应持续下降。
- 出现 `HW Slowdown` / `HW Thermal Slowdown` 等于 `Active`，说明散热或供电有问题，在云上应提工单。
- `SW Power Cap` 处于 Active 属于正常现象：功耗已到上限，驱动在主动限频。

---

## ② 主机 ↔ GPU：PCIe 链路 / H2D·D2H 带宽 / NUMA

### 2.1 安装 nvbandwidth（NVIDIA 官方工具，取代旧的 bandwidthTest）

```bash
sudo apt-get install -y libboost-program-options-dev cmake   # AL2023 用: sudo dnf install -y boost-devel cmake
git clone https://github.com/NVIDIA/nvbandwidth ~/nvbandwidth && cd ~/nvbandwidth
cmake . && make -j
./nvbandwidth -l          # 列出所有 testcase
```

### 2.2 H2D / D2H 带宽

```bash
cd ~/nvbandwidth
./nvbandwidth -t host_to_device_memcpy_ce device_to_host_memcpy_ce   # 每张卡分别测
./nvbandwidth -t host_to_all_memcpy_ce all_to_host_memcpy_ce          # 所有卡同时测，看是否共享上行链路
```

### 2.3 PCIe 链路实际协商速率

```bash
nvidia-smi --query-gpu=index,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv
# 空闲时 GPU 会降到 Gen1 省电，要在压测中查看 current；或者直接看 lspci：
for b in $(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader); do
  b=${b#00000000:}; echo "== $b"; sudo lspci -vv -s $b | egrep 'LnkCap:|LnkSta:'
done
```

### 2.4 NUMA 亲和

```bash
nvidia-smi topo -m          # 看 CPU Affinity / NUMA Affinity 两列
numactl -H                  # NUMA 节点和内存分布
# 同一张卡分别绑本地 NUMA 和远端 NUMA 测试，对比差异：
CUDA_VISIBLE_DEVICES=0 numactl --cpunodebind=0 --membind=0 ./nvbandwidth -t host_to_device_memcpy_ce
CUDA_VISIBLE_DEVICES=0 numactl --cpunodebind=1 --membind=1 ./nvbandwidth -t host_to_device_memcpy_ce
```

判断方法：

| 链路 | 理论单向 | 实际可用（经验）**【推断】** |
|---|---|---|
| Gen4 x8 | 15.75 GB/s | 12–13 GB/s |
| Gen4 x16 | 31.5 GB/s | 24–26 GB/s |
| Gen5 x16 | 63 GB/s | 50–55 GB/s |

- g6e.24xlarge 每卡为 Gen4 x8（`LnkSta` 显示 `x8 (downgraded)`），属于平台设计，不是故障 **【实测】**。详见 [g6e 文档第 2 节](g6e-nccl-allreduce-benchmark.md)。
- 跨 NUMA 访问带宽明显下降时，业务进程（DataLoader、推理服务）要按 GPU 绑定 NUMA。
- 这一层同时决定：模型加载速度、KV cache offload 到 CPU 的速度，以及没有 P2P 时卡间 SHM 通信的上限。

---

## ③ 单机卡间：P2P / NVLink / NCCL 集合通信

### 3.1 拓扑与 P2P 能力

```bash
nvidia-smi topo -m              # NV#=NVLink, PIX/PXB=同 switch, PHB=同 CPU, NODE/SYS=跨 NUMA
nvidia-smi topo -p2p r          # P2P 读能力矩阵：OK / NS(Not Supported) / CNS
nvidia-smi nvlink -s 2>/dev/null | head   # 有 NVLink 的机型
```

### 3.2 P2P 带宽和延迟（CUDA samples）

```bash
git clone --depth 1 https://github.com/NVIDIA/cuda-samples ~/cuda-samples
cd ~/cuda-samples/Samples/5_Domain_Specific/p2pBandwidthLatencyTest
mkdir -p build && cd build && cmake .. && make -j
./p2pBandwidthLatencyTest       # 对比 P2P=Disabled 和 Enabled 两张矩阵
```

或者用 nvbandwidth：

```bash
cd ~/nvbandwidth && ./nvbandwidth -t device_to_device_memcpy_read_ce device_to_device_memcpy_write_ce
```

### 3.3 NCCL 集合通信（nccl-tests）

```bash
git clone https://github.com/NVIDIA/nccl-tests ~/nccl-tests && cd ~/nccl-tests
make -j CUDA_HOME=/usr/local/cuda NCCL_HOME=/usr/lib/x86_64-linux-gnu   # NCCL_HOME 按实际路径填；跨机测试需加 MPI=1 MPI_HOME=/opt/amazon/openmpi
NG=$(nvidia-smi -L | wc -l)
for op in all_reduce all_gather reduce_scatter alltoall broadcast; do
  echo "===== $op"; timeout 300 ./build/${op}_perf -b 8 -e 4G -f 2 -g $NG
done 2>&1 | tee ~/bench/nccl_single_$(hostname).log

# 查看实际走的传输路径（P2P/IPC、SHM、NVLS、NET）
NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,GRAPH,TUNING ./build/all_reduce_perf -b 1G -e 1G -g $NG 2>&1 | egrep 'via|Channel|Ring|Tree' | head -30
```

各操作对应的业务场景：

| 操作 | 典型用途 |
|---|---|
| AllReduce | DP 梯度同步、TP 层输出求和 |
| AllGather / ReduceScatter | FSDP / ZeRO-3 参数和梯度分片 |
| AllToAll | MoE 专家并行（EP）的 token 分发 |
| Broadcast | 参数初始化、权重分发 |

判断方法：
- 看大消息（≥256 MB）的 **busbw**（含义见 [g6e 文档 0.4](g6e-nccl-allreduce-benchmark.md)）和小消息（8 B–64 KB）的 **time (us)**。前者影响训练吞吐，后者影响推理 TPOT。
- 参考数据：
  - g6e.24xlarge 走 SHM，AllReduce busbw 默认约 5 GB/s；设置 `NCCL_SHM_USE_CUDA_MEMCPY=1` 后约 10.26 GB/s **【实测】**。
  - NVLink 机型（p5 等）可达数百 GB/s **【官方量级】**。
- 日志中出现 `via SHM` 说明没有 P2P；出现 `via P2P/IPC` 或 `P2P/CUMEM` 说明 P2P 生效。
- 需要特别注意的是部分 P2P：只有同一 PCIe switch 下的卡之间能 P2P（g7e 疑似如此，见 [g6e 文档 7.2.1](g6e-nccl-allreduce-benchmark.md)）。可以按分组分别测试：

```bash
CUDA_VISIBLE_DEVICES=0,1 ./build/all_reduce_perf -b 1G -e 1G -g 2    # 疑似同 switch
CUDA_VISIBLE_DEVICES=0,2 ./build/all_reduce_perf -b 1G -e 1G -g 2    # 疑似跨 switch
```

---

## ④ 跨机网络：EFA / GPUDirect RDMA

### 4.1 EFA 设备与驱动

```bash
fi_info -p efa -t FI_EP_RDM | egrep 'provider|domain' | sort -u   # 列出 EFA 设备，数量应等于实例规格中的 EFA 接口数
ls /sys/class/infiniband/ 2>/dev/null
cat /opt/amazon/efa_installed_packages 2>/dev/null | head
# 只有实例规格支持 EFA RDMA read/write 时，下面这一项才有意义：
aws ec2 describe-instance-types --instance-types $(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type) \
  --query 'InstanceTypes[0].NetworkInfo.{BW:NetworkPerformance,EFA:EfaSupported,MaxEFA:EfaInfo.MaximumEfaInterfaces}'
```

前置条件：
- 安全组要放行**同一安全组内的全部流量**（入站和出站都要配，EFA 要求）；
- 实例放在 cluster placement group 中。

### 4.2 网卡级别 ping-pong（不经过 GPU）

```bash
# 机器 A（server）
/opt/amazon/efa/bin/fi_pingpong -p efa -e rdm
# 机器 B（client）
/opt/amazon/efa/bin/fi_pingpong -p efa -e rdm <A的私网IP>
```

### 4.3 跨机 NCCL（2 机示例，每机 4 卡）

```bash
cat > ~/hosts <<EOF
<A的私网IP> slots=4
<B的私网IP> slots=4
EOF
/opt/amazon/openmpi/bin/mpirun -np 8 -N 4 --hostfile ~/hosts --bind-to none \
  -x LD_LIBRARY_PATH -x PATH \
  -x FI_PROVIDER=efa -x NCCL_DEBUG=INFO -x NCCL_DEBUG_SUBSYS=INIT,NET \
  ~/nccl-tests/build/all_reduce_perf -b 8 -e 4G -f 2 -g 1 2>&1 | tee ~/bench/nccl_2node.log

grep -E 'NET/OFI|NET/Libfabric|via NET|GDRDMA|Selected provider' ~/bench/nccl_2node.log | sort -u | head
```

判断方法：
- `NET/OFI Selected provider is efa`：说明走了 EFA。如果是 `NET/Socket`，说明 aws-ofi-nccl 插件没加载上，带宽会很低。
- `via NET/Libfabric/<n>/GDRDMA`：说明 GPUDirect RDMA 生效。没有 `GDRDMA` 表示数据要经过主机内存中转。
- g6e 不支持跨机 GDR，**不要**手动设置 `FI_EFA_USE_DEVICE_RDMA=1`，否则会 abort；社区实测约 3 GB/s **【社区，未自测】**。
- g7e 多卡规格、p4d/p5/p6 支持 GDR **【官方】**。
- 跨机 busbw 的上限约为 网卡总带宽（Gbps）÷ 8 × 0.8–0.9 **【推断】**。

---

## ⑤ 存储：训练数据 / 模型加载 / checkpoint

### 5.1 本地 NVMe（instance store）

```bash
lsblk -d -o NAME,SIZE,MODEL | grep -i nvme
# 多块盘先做 RAID0（会清空数据！）：sudo mdadm --create /dev/md0 --level=0 --raid-devices=N /dev/nvme1n1 ... && sudo mkfs.xfs /dev/md0 && sudo mount /dev/md0 /mnt/nvme
sudo fio --name=seqread --filename=/mnt/nvme/fio.test --size=20G --rw=read  --bs=1M --iodepth=64 --numjobs=8 --ioengine=libaio --direct=1 --group_reporting
sudo fio --name=seqwrite --filename=/mnt/nvme/fio.test --size=20G --rw=write --bs=1M --iodepth=64 --numjobs=8 --ioengine=libaio --direct=1 --group_reporting
sudo fio --name=randread --filename=/mnt/nvme/fio.test --size=20G --rw=randread --bs=4k --iodepth=128 --numjobs=8 --ioengine=libaio --direct=1 --group_reporting
```

### 5.2 共享存储（FSx for Lustre / EFS）

```bash
# 多台机器同时跑，测聚合带宽
fio --name=fsx --directory=/fsx/bench_$(hostname) --size=10G --rw=read --bs=1M --iodepth=32 --numjobs=16 --ioengine=libaio --direct=1 --group_reporting
lfs getstripe /fsx 2>/dev/null     # Lustre 条带化设置会影响大文件带宽
```

### 5.3 S3 吞吐（模型和数据集下载）

```bash
# s5cmd 并发高，比 aws s3 cp 快得多
curl -sL https://github.com/peak/s5cmd/releases/download/v2.3.0/s5cmd_2.3.0_Linux-64bit.tar.gz | tar xz -C /tmp && sudo mv /tmp/s5cmd /usr/local/bin/
time s5cmd --numworkers 64 cp 's3://<bucket>/<model-prefix>/*' /mnt/nvme/model/
```

### 5.4 GPUDirect Storage（可选，仅支持 GDS 的机型和文件系统）

```bash
/usr/local/cuda/gds/tools/gdscheck -p                       # 检查 GDS 是否可用
/usr/local/cuda/gds/tools/gdsio -D /mnt/nvme -d 0 -w 8 -s 4G -i 1M -x 0 -I 0   # -x 0 走 GPU_DIRECT，-x 1 走 CPU 中转，两者对比
```

判断方法：
- 业务上最直观的指标是**模型冷启动时长**，即从 S3 或 FSx 加载到所有 GPU 可服务为止。
- 训练时 checkpoint 写入会造成停顿，写入时间应远小于 checkpoint 间隔。

---

## ⑥ 端到端业务：推理 / 训练

### 6.1 推理（vLLM）

```bash
# 启动服务（示例为 TP=4）
vllm serve <model> --tensor-parallel-size 4 --max-model-len 8192 --port 8000 &
# 压测：固定输入/输出长度，扫描并发
for c in 1 4 16 64; do
  vllm bench serve --backend vllm --model <model> --base-url http://localhost:8000 \
    --dataset-name random --random-input-len 1024 --random-output-len 256 \
    --num-prompts $((c*10)) --max-concurrency $c 2>&1 | egrep 'Concurrency|throughput|TTFT|TPOT|ITL' | sed "s/^/[c=$c] /"
done | tee ~/bench/vllm_$(hostname).log
```

（旧版本 vLLM 用 `python benchmarks/benchmark_serving.py`，参数基本相同。）

关注的指标：
- **TTFT**（首 token 延迟）：主要受 prefill 算力影响，对应 ①；
- **TPOT / ITL**（每个输出 token 的延迟）：主要受显存带宽影响（①），TP 时还受小消息 AllReduce 延迟影响（③）；
- **吞吐（tokens/s）**：用于和成本对比，算出 $/百万 token。
- 对比部署方式：同样卡数下 TP=4 与 4 × TP=1 多副本。在没有 NVLink 的机型上，多副本往往吞吐更高 **【推断，需实测】**。

### 6.2 训练（吞吐与 MFU）

```bash
# 任选标准 benchmark：Megatron-LM / torchtitan / NeMo，跑固定步数，记录 tokens/s/GPU
# torchtitan 示例：
git clone https://github.com/pytorch/torchtitan ~/torchtitan && cd ~/torchtitan && pip install -r requirements.txt
NGPU=$(nvidia-smi -L | wc -l) CONFIG_FILE=./torchtitan/models/llama3/train_configs/llama3_8b.toml ./run_train.sh --training.steps 50
# 日志里的 tps（tokens/s）和 mfu 就是目标指标
```

MFU 估算：MFU ≈ 6 × 参数量 × tokens/s ÷ (GPU 数 × 单卡峰值 FLOPS)。dense 模型在 NVLink 集群上一般为 35%–55% **【业界经验】**。PCIe 机型因为通信瓶颈，MFU 会明显更低。

### 6.3 扩展效率（scaling efficiency）

先在 1 机上跑出吞吐 T₁，再在 N 机上跑出吞吐 T_N：

扩展效率 = T_N ÷ (N × T₁)

一般 > 90% 为优秀，< 70% 时要回头查 ④ **【业界经验】**。

---

## ⑦ 稳定性与健康

### 7.1 DCGM 诊断（最推荐，NVIDIA 官方）

```bash
# 安装：sudo apt-get install -y datacenter-gpu-manager（或 datacenter-gpu-manager-4-cuda12）；sudo systemctl start nvidia-dcgm
dcgmi discovery -l
dcgmi diag -r 1      # 快速检查，约 1 分钟
dcgmi diag -r 3      # 中等强度，约 10–30 分钟，验收建议跑这个级别
# -r 4 为长时间压测（约 1 小时以上），新集群验收或怀疑硬件问题时使用
```

### 7.2 ECC / XID / 降频

```bash
nvidia-smi -q -d ECC | egrep -i 'Volatile|Aggregate|Uncorr|Corr|Retired|Remapp' | head -30
nvidia-smi -q -d ROW_REMAPPER 2>/dev/null | head -20
sudo dmesg -T | grep -i xid           # 出现任何 XID 都要记录。常见：13/31=应用错误，48/63/64/94/95=显存 ECC，79=掉卡
nvidia-smi --query-gpu=index,clocks_event_reasons.active --format=csv   # 旧驱动用 clocks_throttle_reasons.active
```

### 7.3 长时间稳定性

```bash
# 至少跑 2–4 小时的满载，期间持续采样（结合 1.3 的 dmon）
cd ~/gpu-burn && ./gpu_burn -tc -d 7200 2>&1 | tail -5
# 通信稳定性：循环跑 nccl-tests，检查结果是否有波动，也能看是否会卡住
for i in $(seq 1 50); do timeout 300 ~/nccl-tests/build/all_reduce_perf -b 1G -e 1G -g $NG -n 50 | grep -E '^\s+1073741824' ; done | tee ~/bench/nccl_loop.log
```

判断方法：
- dcgmi diag 全部为 `Pass`；
- 压测过程中无新的 XID，ECC 计数不增长；
- nccl 循环测试中 busbw 波动 < 5%，且没有出现 `timeout`（exit 124）。

---

## 8. 一键汇总脚本（单机，①②③⑦ 快速版）

```bash
cat > ~/bench/quick_bench.sh <<'EOF'
#!/bin/bash
# 依赖：~/bench/matmul.py、~/bench/membw.py、~/nvbandwidth、~/nccl-tests 已就绪
set -u; cd ~/bench; H=$(hostname); NG=$(nvidia-smi -L | wc -l); OUT=quick_${H}_$(date +%m%d%H%M).log
{
echo "##### ⑦ health";   nvidia-smi -q -d ECC | grep -i -m4 uncorr; sudo dmesg -T | grep -ic xid
echo "##### ① matmul";   python matmul.py bf16
echo "##### ① membw";    python membw.py
echo "##### ② h2d/d2h";  ~/nvbandwidth/nvbandwidth -t host_to_device_memcpy_ce device_to_host_memcpy_ce | grep -A6 -E 'memcpy CE'
echo "##### ③ topo";     nvidia-smi topo -m; nvidia-smi topo -p2p r
echo "##### ③ d2d";      ~/nvbandwidth/nvbandwidth -t device_to_device_memcpy_read_ce | grep -A$((NG+2)) 'memcpy CE'
for op in all_reduce all_gather alltoall; do
  echo "##### ③ nccl $op"; timeout 300 ~/nccl-tests/build/${op}_perf -b 8 -e 4G -f 4 -g $NG | egrep -v '^#\s*$'
done
echo "##### ③ nccl all_reduce + SHM_USE_CUDA_MEMCPY=1"
NCCL_SHM_USE_CUDA_MEMCPY=1 timeout 300 ~/nccl-tests/build/all_reduce_perf -b 8 -e 4G -f 4 -g $NG | egrep -v '^#\s*$'
} 2>&1 | tee $OUT
echo "saved: ~/bench/$OUT"
EOF
chmod +x ~/bench/quick_bench.sh && ~/bench/quick_bench.sh
```

跑完后把 `~/bench/quick_*.log` 发回来分析即可。

---

## 9. 验收记录模板

| 层 | 项目 | 机型 A | 机型 B | 参考/期望 | 结论 |
|---|---|---|---|---|---|
| ① | BF16 matmul TFLOPS（每卡） | | | 峰值 × 60–75% | |
| ① | 显存 copy GB/s | | | 峰值 × 75–90% | |
| ① | 满载功耗 / 是否降频 | | | ≈TDP / 否 | |
| ② | PCIe Gen × 宽度 | | | | |
| ② | H2D / D2H GB/s | | | 见 ② 表 | |
| ③ | P2P 矩阵 | | | OK / NS / 部分 | |
| ③ | AllReduce busbw @1GB | | | | |
| ③ | AllReduce 8B 延迟 (us) | | | | |
| ③ | AllToAll busbw @1GB | | | | |
| ④ | provider / GDRDMA | | | efa / 是否 | |
| ④ | 2 机 AllReduce busbw | | | 网卡带宽 × 0.8 | |
| ⑤ | NVMe 顺序读 GB/s | | | | |
| ⑤ | 模型冷启动时长 | | | | |
| ⑥ | TTFT / TPOT @c=16 | | | | |
| ⑥ | 吞吐 tokens/s · $/M tokens | | | | |
| ⑥ | 训练 MFU / 扩展效率 | | | | |
| ⑦ | dcgmi diag -r 3 | | | Pass | |
| ⑦ | XID / ECC 增量 | | | 0 | |

---

## 10. 容易忽略的点

1. **先做健康检查再测性能**：一张坏卡会把整个 AllReduce 拖慢，因为集合通信受最慢的卡限制。
2. **PCIe 链路状态要在负载下看**：空闲时 GPU 会降到 Gen1，看起来像"降级"。
3. **容器中运行时**：要加 `--ipc=host` 或调大 `--shm-size`，否则 NCCL 的 SHM 传输会失败或退化。K8s 用 Memory 类型的 emptyDir 挂到 `/dev/shm`。
4. **固定软件版本**：driver、CUDA、NCCL、aws-ofi-nccl、torch 的版本都要记录（见第 0 节）。版本不同时，测试结果不可比。
5. **busbw 和 algbw 不要混用**：对比时统一使用 busbw。
6. **小消息延迟比大消息带宽更影响推理**：只看 4 GB 的 busbw 会误判 TP 推理的效果。
7. **跨机测试前先测单机**：单机不达标时，跨机数据没有意义。
8. **记录实例 ID、AZ 和 placement group**：同一机型在不同 AZ、不同批次之间可能有差异。

---

## 待办 / 未验证

- [ ] 在 g6e.24xlarge 上跑第 8 节 quick_bench，补齐 ①② 的实测值
- [ ] 在 g7e 上跑 ③ 的分组测试，验证跨 switch 时 P2P 是否被禁用
- [ ] 补测 2 台 g6e 的跨机 NCCL（验证社区给出的约 3 GB/s）
- [ ] 推理场景下 TP=4 与 4 × TP=1 吞吐的实测对比
