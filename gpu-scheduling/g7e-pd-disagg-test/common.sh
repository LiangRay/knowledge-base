#!/bin/bash
# 公共变量，被其它脚本 source
export VENV=${VENV:-$HOME/pd-venv}
export WORK=${WORK:-$HOME/pd-test}
export RESULTS=${RESULTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/results}
export MODEL=${MODEL:-Qwen/Qwen3-8B}
export MAX_LEN=${MAX_LEN:-20480}
export PROXY_PORT=${PROXY_PORT:-8000}
export P_PORT=${P_PORT:-8100}
export D_PORT=${D_PORT:-8200}
if [ -z "${HF_HOME:-}" ]; then [ -d /opt/dlami/nvme ] && export HF_HOME=/opt/dlami/nvme/hf || export HF_HOME=$HOME/.cache/huggingface; fi
mkdir -p "$RESULTS" "$WORK" "$HF_HOME"
[ -f "$VENV/bin/activate" ] && source "$VENV/bin/activate"
log(){ echo "[$(date +%H:%M:%S)] $*"; }
wait_http(){ # url timeout_s
  local u=$1 t=${2:-900} s=0
  until curl -sf "$u" >/dev/null; do sleep 5; s=$((s+5)); [ $s -ge $t ] && { log "timeout waiting $u"; return 1; }; done
  log "ready: $u"
}
