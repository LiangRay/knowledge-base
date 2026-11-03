#!/bin/bash
# 在每台 g7e 上执行：venv + vLLM + NIXL + nccl-tests + vLLM disagg proxy
set -euo pipefail
cd "$(dirname "$0")"; source ./common.sh
VLLM_VER=${VLLM_VER:-}          # 留空 = 最新；报告里记录实际版本
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=$HOME/.local/bin:$PATH
[ -d "$VENV" ] || uv venv "$VENV" --python 3.12
source "$VENV/bin/activate"
uv pip install "vllm${VLLM_VER:+==$VLLM_VER}" nixl quart httpx aiohttp pandas
# vLLM 官方 PD 示例 proxy（toy_proxy_server.py）
if [ ! -d "$WORK/vllm-src" ]; then
  git clone --depth 1 --filter=blob:none --sparse https://github.com/vllm-project/vllm "$WORK/vllm-src"
  (cd "$WORK/vllm-src" && git sparse-checkout set tests/v1/kv_connector/nixl_integration benchmarks)
fi
# nccl-tests（用 DLAMI 自带 CUDA / NCCL / aws-ofi-nccl）
if [ ! -x "$WORK/nccl-tests/build/all_reduce_perf" ]; then
  git clone --depth 1 https://github.com/NVIDIA/nccl-tests "$WORK/nccl-tests"
  CUDA_HOME=${CUDA_HOME:-/usr/local/cuda}
  make -C "$WORK/nccl-tests" -j MPI=1 MPI_HOME=/opt/amazon/openmpi CUDA_HOME=$CUDA_HOME NCCL_HOME=${NCCL_HOME:-$CUDA_HOME} >/dev/null
fi
python -c "import vllm,nixl;print('vllm',vllm.__version__);print('nixl ok')" | tee "$RESULTS/versions_$(hostname).txt"
pip show nixl 2>/dev/null | grep -i version | tee -a "$RESULTS/versions_$(hostname).txt"
log "install done. source $VENV/bin/activate"
