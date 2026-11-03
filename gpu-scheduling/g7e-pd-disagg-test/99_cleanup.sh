#!/bin/bash
pkill -f "vllm serve" ; pkill -f toy_proxy_server ; pkill -f nixlbench
docker rm -f nixl-etcd 2>/dev/null
sleep 5; nvidia-smi --query-gpu=index,memory.used --format=csv
