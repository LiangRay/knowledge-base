"""确定性测试：先拿到一个含 reasoning 的响应，然后把它原样回放 N 次，看每次落到哪个后端、成功与否。"""
import os
import json, sys, requests, collections
BASE = "http://127.0.0.1:4100"; N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
H = {"Authorization": "Bearer " + os.environ.get("LITELLM_KEY", "sk-change-me")}
TOOLS = [{"type": "function", "name": "calc", "description": "计算算式", "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}]
def post(inp):
    r = requests.post(f"{BASE}/v1/responses", headers=H, timeout=300, json={"model": "gpt-lb", "store": False,
        "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "high"}, "tools": TOOLS, "input": inp})
    return r.status_code, r.headers.get("x-litellm-model-id"), (r.json() if r.status_code == 200 else r.text[:160])
q = [{"role": "user", "content": "一个农夫有17只羊，除了9只以外都死了；他又买了比剩下数量多一倍再少3只的羊。先想清楚，再用calc工具算最终数量。"}]
for _ in range(6):
    code, be, js = post(q)
    out = js["output"] if code == 200 else []
    if any(o.get("type") == "reasoning" and o.get("encrypted_content") for o in out) and any(o.get("type") == "function_call" for o in out):
        break
else:
    sys.exit("没拿到 reasoning+function_call")
print("turn1 by", be, "items:", [o["type"] for o in out])
fc = next(o for o in out if o["type"] == "function_call")
hist = q + [{k: v for k, v in o.items() if k != "status"} for o in out] + [{"type": "function_call_output", "call_id": fc["call_id"], "output": "15"}]
c = collections.Counter()
for i in range(N):
    code, be2, js = post(hist)
    same = "same" if be2 == be else "CROSS"
    c[(same, code)] += 1
    txt = (js.get("output_text") or "")[:40] if code == 200 else js[:110]
    print(f"replay{i} -> {be2} [{same}] {code} {txt!r}")
print(dict(c))
