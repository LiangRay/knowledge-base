#!/bin/bash
# 扫描 输入长度 × 请求速率，结果写 results/bench_<TAG>/
cd "$(dirname "$0")"; source ./common.sh
TAG=${TAG:-$(cat $RESULTS/logs_*/config.txt 2>/dev/null | tail -1 | awk '{print $1}')}
BASE=${BASE:-http://localhost:$PROXY_PORT}
OUT_LEN=${OUT_LEN:-256}
if [ "${QUICK:-0}" = 1 ]; then IN_LENS="1024"; RATES="2"; NUM=20
else IN_LENS=${IN_LENS:-"1024 4096 16384"}; RATES=${RATES:-"1 2 4 8 inf"}; NUM=${NUM:-200}; fi
D=$RESULTS/bench_$TAG; mkdir -p "$D"
for L in $IN_LENS; do for R in $RATES; do
  log "$TAG in=$L out=$OUT_LEN rate=$R"
  vllm bench serve --backend vllm --model $MODEL --base-url $BASE --endpoint /v1/completions \
    --dataset-name random --random-input-len $L --random-output-len $OUT_LEN --ignore-eos \
    --num-prompts $NUM --request-rate $R --seed 42 \
    --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,90,99 \
    --save-result --result-dir "$D" --result-filename "in${L}_r${R}.json" 2>&1 | tail -25 > "$D/in${L}_r${R}.txt"
done; done
log "done -> $D ；汇总：python3 40_summarize.py $RESULTS"
