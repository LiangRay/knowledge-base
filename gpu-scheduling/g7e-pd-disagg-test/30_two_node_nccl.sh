#!/bin/bash
# S2：2 机 × 8 卡 nccl-tests，检查是否走 EFA + GDR。在 node A 上执行，需 A→B 免密 ssh。
cd "$(dirname "$0")"; source ./common.sh
: "${NODE_B:?需要 NODE_B=<B私网IP>}"
NODE_A=${NODE_A:-$(hostname -I | awk '{print $1}')}
GPN=${GPN:-8}
OUT=$RESULTS/nccl_2node.txt
LIB=/opt/amazon/ofi-nccl/lib:/opt/amazon/efa/lib:/opt/amazon/openmpi/lib:/usr/local/cuda/lib64
[ -d /opt/amazon/ofi-nccl/lib ] || LIB=/opt/aws-ofi-nccl/lib:$LIB
/opt/amazon/openmpi/bin/mpirun -np $((GPN*2)) -H $NODE_A:$GPN,$NODE_B:$GPN --bind-to none \
  -x LD_LIBRARY_PATH=$LIB:${LD_LIBRARY_PATH:-} -x FI_PROVIDER=efa -x NCCL_DEBUG=INFO -x NCCL_DEBUG_SUBSYS=INIT,NET \
  --mca pml ^cm,ucx --mca btl tcp,self --mca btl_tcp_if_exclude lo,docker0 \
  $WORK/nccl-tests/build/all_reduce_perf -b 8 -e 8G -f 2 -g 1 -n 20 > "$OUT" 2>&1
echo "== transport =="; grep -oE "via NET/[A-Za-z]+/[0-9]+(/GDRDMA)?" "$OUT" | sort | uniq -c
grep -q GDRDMA "$OUT" && echo ">>> GDR: YES" || echo ">>> GDR: NO（没有 GDRDMA 字样）"
grep -E "NET/OFI|Selected Provider|aws-ofi-nccl" "$OUT" | head -5
grep -E "^ +(8|1048576|67108864|1073741824|8589934592) |Avg bus" "$OUT"
