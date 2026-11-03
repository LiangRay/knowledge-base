#!/bin/bash
# S3 跨机 1P1D：ROLE=decode（在 B）| prefill（在 A，同时起 proxy，需要 NODE_B）
set -euo pipefail
cd "$(dirname "$0")"; source ./common.sh
ROLE=${ROLE:?ROLE=prefill|decode}; GPUS=${GPUS:-0,1}; TP=${TP:-2}
NIXL_BACKEND=${NIXL_BACKEND:-UCX}
MYIP=$(hostname -I | awk '{print $1}')
LOGD=$RESULTS/logs_xnode-${NIXL_BACKEND,,}; mkdir -p "$LOGD"
echo "xnode-${NIXL_BACKEND,,} model=$MODEL tp=$TP gpus=$GPUS" > "$LOGD/config.txt"
KV_CFG="{\"kv_connector\":\"NixlConnector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"backends\":[\"$NIXL_BACKEND\"]}}"
# EFA 相关：UCX 后端时让 UCX 能看到 EFA 以外的 tcp/cuda 传输；LIBFABRIC 后端时指定 efa provider
export FI_PROVIDER=efa FI_EFA_FORK_SAFE=1
PORT=$([ $ROLE = prefill ] && echo $P_PORT || echo $D_PORT)
CUDA_VISIBLE_DEVICES=$GPUS VLLM_NIXL_SIDE_CHANNEL_HOST=$MYIP VLLM_NIXL_SIDE_CHANNEL_PORT=5600 \
  nohup vllm serve $MODEL --host 0.0.0.0 --port $PORT -tp $TP --max-model-len $MAX_LEN \
  --gpu-memory-utilization 0.85 --kv-transfer-config "$KV_CFG" \
  > "$LOGD/$ROLE.log" 2>&1 &
wait_http http://localhost:$PORT/v1/models 1800
if [ $ROLE = prefill ]; then
  : "${NODE_B:?需要 NODE_B}"
  wait_http http://$NODE_B:$D_PORT/v1/models 1800
  nohup python "$WORK/vllm-src/tests/v1/kv_connector/nixl_integration/toy_proxy_server.py" --port $PROXY_PORT \
    --prefiller-hosts localhost --prefiller-ports $P_PORT --decoder-hosts $NODE_B --decoder-ports $D_PORT \
    > "$LOGD/proxy.log" 2>&1 &
  sleep 5
  curl -s localhost:$PROXY_PORT/v1/completions -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"San Francisco is a\",\"max_tokens\":32}"; echo
  log "跑 TAG=xnode-${NIXL_BACKEND,,} ./20_bench.sh"
fi
