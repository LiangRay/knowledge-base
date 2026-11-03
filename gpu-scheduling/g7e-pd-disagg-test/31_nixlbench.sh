#!/bin/bash
# S2：NIXL 微基准（nixlbench）。ROLE=etcd（仅 A）| worker（A、B 各一个，ETCD=<A私网IP>）
# 注意：nixlbench 需要从 NIXL 源码 benchmark/nixlbench 编译，参数随版本变化，以 --help 为准。
set -euo pipefail
cd "$(dirname "$0")"; source ./common.sh
ROLE=${ROLE:-worker}; BACKEND=${BACKEND:-LIBFABRIC}
if [ "$ROLE" = etcd ]; then
  docker run -d --rm --name nixl-etcd --net host quay.io/coreos/etcd:v3.5.13 /usr/local/bin/etcd \
    --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls http://$(hostname -I|awk '{print $1}'):2379
  log "etcd started"; exit 0
fi
: "${ETCD:?需要 ETCD=<A私网IP>}"
NB=$(command -v nixlbench || echo $WORK/nixl/build/benchmark/nixlbench/nixlbench)
if [ ! -x "$NB" ]; then
  log "未找到 nixlbench，请按 https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench 编译（需要 meson/ninja/etcd-cpp-api，建议用其 Dockerfile）"; exit 1
fi
for SEG in VRAM DRAM; do
  CUDA_VISIBLE_DEVICES=${GPU:-0} $NB --etcd_endpoints http://$ETCD:2379 --backend $BACKEND \
    --initiator_seg_type $SEG --target_seg_type $SEG \
    --start_block_size 4096 --max_block_size 67108864 --start_batch_size 1 --max_batch_size 1 \
    2>&1 | tee "$RESULTS/nixlbench_${BACKEND}_${SEG}_$(hostname).txt"
done
