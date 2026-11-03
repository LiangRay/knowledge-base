#!/bin/bash
# S0 环境体检：拓扑 / P2P 分组 / PCIe / EFA / GDR 前置条件
cd "$(dirname "$0")"; source ./common.sh
OUT=$RESULTS/env_$(hostname).txt
{
echo "### $(date -Is) $(hostname) $(curl -s -m2 http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null)"
echo "## driver / cuda"; nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv
nvcc --version 2>/dev/null | tail -1
echo "## topo -m"; nvidia-smi topo -m
echo "## topo -p2p r"; nvidia-smi topo -p2p r
echo "## PCIe link"; nvidia-smi --query-gpu=index,pci.bus_id,pcie.link.gen.max,pcie.link.width.max,pcie.link.width.current --format=csv
echo "## modules"; lsmod | grep -E "^efa|nvidia_peermem|gdrdrv" || echo "(none)"
echo "## EFA"; fi_info -p efa -t FI_EP_RDM 2>/dev/null | grep -E "provider|domain" | sort -u || echo "fi_info not found"
ls /sys/class/infiniband 2>/dev/null
echo "## EFA NUMA"; for d in /sys/class/infiniband/*; do echo "$(basename $d) numa=$(cat $d/device/numa_node 2>/dev/null)"; done
echo "## versions"
ls /opt/amazon/efa/lib*/libfabric.so* 2>/dev/null | head -2
ls -d /opt/amazon/ofi-nccl* /opt/aws-ofi-nccl* 2>/dev/null
python -c "import torch;print('torch',torch.__version__,'nccl',torch.cuda.nccl.version())" 2>/dev/null
echo "## GPU NUMA"; for b in $(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader); do d=$(echo "$b" | tr A-Z a-z | sed 's/^0000//'); echo "$b numa=$(cat /sys/bus/pci/devices/$d/numa_node 2>/dev/null)"; done
} 2>&1 | tee "$OUT"

# P2P 分组：根据 topo -p2p r 中 OK 的连通关系分组
python3 - "$OUT" <<'EOF' | tee -a "$OUT"
import re,sys
t=open(sys.argv[1]).read()
sec=t.split("## topo -p2p r")[1].split("## PCIe")[0]
rows=[l.split() for l in sec.splitlines() if re.match(r"\s*GPU\d+\s+(X|OK|NS|CNS|TNS|GNS|NS)\b",l)]
n=len(rows); adj={i:set() for i in range(n)}
for r in rows:
    i=int(r[0][3:]); vals=r[1:1+n]
    for j,v in enumerate(vals):
        if v=="OK": adj[i].add(j)
seen=set(); groups=[]
for i in range(n):
    if i in seen: continue
    g={i}|adj[i]; seen|=g; groups.append(sorted(g))
print("## P2P groups (from topo -p2p r):", groups)
print("   => TP 组和同机 P/D 摆放请对齐这些分组")
EOF

# P2P 带宽矩阵（有 nvbandwidth 就用，否则跳过）
if command -v nvbandwidth >/dev/null; then
  nvbandwidth -t device_to_device_memcpy_read_ce | tee -a "$OUT"
else
  log "nvbandwidth 未安装，跳过 P2P 带宽矩阵（见 gpu-cluster-benchmark-checklist.md 2.1）"
fi
log "saved $OUT"
