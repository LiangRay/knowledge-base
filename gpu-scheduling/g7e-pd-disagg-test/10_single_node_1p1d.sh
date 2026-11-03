#!/bin/bash
# S1 同机：MODE = baseline | pd-same-switch | pd-cross-switch | pd-4x4 | custom
# custom: P_GPUS=0,1 D_GPUS=2,3 TP=2
set -euo pipefail
cd "$(dirname "$0")"; source ./common.sh
MODE=${MODE:-pd-same-switch}
case $MODE in
  baseline)        B_GPUS=${B_GPUS:-0,1,2,3}; TP=${TP:-2};;
  pd-same-switch)  P_GPUS=${P_GPUS:-0,1}; D_GPUS=${D_GPUS:-2,3}; TP=${TP:-2};;
  pd-cross-switch) P_GPUS=${P_GPUS:-0,1}; D_GPUS=${D_GPUS:-4,5}; TP=${TP:-2};;
  pd-4x4)          P_GPUS=${P_GPUS:-0,1,2,3}; D_GPUS=${D_GPUS:-4,5,6,7}; TP=${TP:-4};;
  custom)          : "${P_GPUS:?}" "${D_GPUS:?}" "${TP:?}";;
  *) echo "unknown MODE"; exit 1;;
esac
NIXL_BACKEND=${NIXL_BACKEND:-UCX}
LOGD=$RESULTS/logs_$MODE; mkdir -p "$LOGD"
COMMON_ARGS="--max-model-len $MAX_LEN --gpu-memory-utilization 0.85"
echo "$MODE model=$MODEL tp=$TP P=${P_GPUS:-} D=${D_GPUS:-} B=${B_GPUS:-} backend=$NIXL_BACKEND" > "$LOGD/config.txt"

if [ "$MODE" = baseline ]; then
  # 不分离：同样的卡数，DP=卡数/TP
  DP=$(( $(echo $B_GPUS | tr , '\n' | wc -l) / TP ))
  CUDA_VISIBLE_DEVICES=$B_GPUS nohup vllm serve $MODEL --port $PROXY_PORT -tp $TP --data-parallel-size $DP $COMMON_ARGS \
    > "$LOGD/baseline.log" 2>&1 &
  wait_http http://localhost:$PROXY_PORT/v1/models 1800
  exit 0
fi

KV_CFG="{\"kv_connector\":\"NixlConnector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"backends\":[\"$NIXL_BACKEND\"]}}"
# Prefill
CUDA_VISIBLE_DEVICES=$P_GPUS VLLM_NIXL_SIDE_CHANNEL_PORT=5600 UCX_LOG_LEVEL=info \
  nohup vllm serve $MODEL --port $P_PORT -tp $TP $COMMON_ARGS --kv-transfer-config "$KV_CFG" \
  > "$LOGD/prefill.log" 2>&1 &
# Decode
CUDA_VISIBLE_DEVICES=$D_GPUS VLLM_NIXL_SIDE_CHANNEL_PORT=5700 UCX_LOG_LEVEL=info \
  nohup vllm serve $MODEL --port $D_PORT -tp $TP $COMMON_ARGS --kv-transfer-config "$KV_CFG" \
  > "$LOGD/decode.log" 2>&1 &
wait_http http://localhost:$P_PORT/v1/models 1800
wait_http http://localhost:$D_PORT/v1/models 1800
# Proxy（vLLM 示例：先发 P 做 prefill(max_tokens=1)，再把 KV 元数据交给 D）
nohup python "$WORK/vllm-src/tests/v1/kv_connector/nixl_integration/toy_proxy_server.py" --port $PROXY_PORT \
  --prefiller-hosts localhost --prefiller-ports $P_PORT --decoder-hosts localhost --decoder-ports $D_PORT \
  > "$LOGD/proxy.log" 2>&1 &
sleep 5
curl -s localhost:$PROXY_PORT/v1/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"San Francisco is a\",\"max_tokens\":32}" | tee "$LOGD/smoke.json"; echo
log "KV 传输线索："; grep -hiE "nixl|ucx|transport|cuda_ipc|backend" "$LOGD"/{prefill,decode}.log | sort -u | head -20
