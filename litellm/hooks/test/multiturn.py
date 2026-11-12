"""
通过 LiteLLM（4100 端口）模拟 Codex 的多轮工具调用：
每轮把上一轮的 output（含 reasoning/encrypted_content）原样回放，统计成功率与路由情况。
"""
import os
import json, sys, requests, collections
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4100"
N_CONV = int(sys.argv[2]) if len(sys.argv) > 2 else 6
STREAM = (sys.argv[3] == "stream") if len(sys.argv) > 3 else False
H = {"Authorization": "Bearer " + os.environ.get("LITELLM_KEY", "sk-change-me"), "Content-Type": "application/json"}
TOOLS = [{"type": "function", "name": "get_weather", "description": "查询城市天气",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
CITIES = ["北京", "上海", "广州"]

def post(inp):
    body = {"model": "gpt-lb", "store": False, "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "medium"}, "tools": TOOLS, "input": inp, "stream": STREAM}
    r = requests.post(f"{BASE}/v1/responses", headers=H, json=body, timeout=300, stream=STREAM)
    backend = r.headers.get("x-litellm-model-id")
    if r.status_code != 200:
        return r.status_code, backend, r.text[:200]
    if not STREAM:
        return 200, backend, r.json()["output"]
    out = []
    for line in r.iter_lines():
        if line.startswith(b"data: "):
            try: ev = json.loads(line[6:])
            except Exception: continue
            if ev.get("type") == "response.output_item.done":
                out.append(ev["item"])
    return 200, backend, out

stats = collections.Counter()
for c in range(N_CONV):
    inp = [{"role": "user", "content": "依次调用工具查询 北京、上海、广州 天气（每次只调一个），全部查完后一句话总结。"}]
    path, ok = [], True
    for turn in range(5):
        code, be, out = post(inp)
        path.append(f"{be}:{code}")
        stats["req"] += 1
        if code != 200:
            stats["fail"] += 1; ok = False
            print(f"  conv{c} turn{turn} FAIL {code} {out}")
            break
        n_r = sum(1 for o in out if o.get("type") == "reasoning")
        tagged = sum(1 for o in out if o.get("type") == "reasoning" and str(o.get("encrypted_content", "")).startswith("litellm_enc:"))
        stats["reasoning_items"] += n_r; stats["tagged"] += tagged
        fcs = [o for o in out if o.get("type") == "function_call"]
        clean = []
        for o in out:
            o = dict(o); o.pop("status", None); clean.append(o)
        inp = inp + clean
        if not fcs:
            break
        for fc in fcs:
            city = json.loads(fc.get("arguments") or "{}").get("city", "?")
            inp.append({"type": "function_call_output", "call_id": fc["call_id"], "output": json.dumps({"city": city, "weather": "晴 20℃"}, ensure_ascii=False)})
    stats["conv_ok" if ok else "conv_fail"] += 1
    print(f"conv{c} {'OK ' if ok else 'ERR'} {' -> '.join(path)}")
print(dict(stats))
