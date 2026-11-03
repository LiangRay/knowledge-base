#!/usr/bin/env python3
"""汇总 results/bench_*/in*_r*.json 为 Markdown 表。用法：python3 40_summarize.py results/"""
import json, glob, os, re, sys
root = sys.argv[1] if len(sys.argv) > 1 else "results"
rows = []
for f in sorted(glob.glob(os.path.join(root, "bench_*", "in*_r*.json"))):
    tag = os.path.basename(os.path.dirname(f))[6:]
    m = re.match(r"in(\d+)_r(.+)\.json", os.path.basename(f))
    d = json.load(open(f))
    g = lambda k: d.get(k, float("nan"))
    rows.append((tag, int(m.group(1)), m.group(2),
                 g("p50_ttft_ms"), g("p99_ttft_ms"), g("p50_tpot_ms"), g("p99_tpot_ms"),
                 g("output_throughput"), g("completed")))
rows.sort(key=lambda r: (r[1], r[2], r[0]))
print("| 配置 | 输入 | 速率 | TTFT P50 | TTFT P99 | TPOT P50 | TPOT P99 | 输出 tok/s | 完成数 |")
print("|---|---|---|---|---|---|---|---|---|")
for r in rows:
    print("| %s | %d | %s | %.0f | %.0f | %.1f | %.1f | %.0f | %s |" % r)
