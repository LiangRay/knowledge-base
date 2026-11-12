"""
模拟两个"不同加密边界"的上游（例如两个 OpenAI org，或两个 Azure resource）。
请求实际转发到 Bedrock Runtime /openai/v1/responses（SigV4）。
每个后端只认自己签发过的 encrypted_content，遇到别家签发的直接返回
400 invalid_encrypted_content（与 OpenAI 跨 org 时的报错一致）。
用法：python3 strict_upstream.py 18001 A  /  python3 strict_upstream.py 18002 B
"""
import json, sys, hashlib, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import boto3, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

PORT, NAME = int(sys.argv[1]), sys.argv[2]
REGION = "us-east-1"
URL = f"https://bedrock-runtime.{REGION}.amazonaws.com/openai/v1/responses"
sess = boto3.Session()
issued, lock = set(), threading.Lock()
h = lambda s: hashlib.sha256(s.encode()).hexdigest()

def collect(obj, out):
    if isinstance(obj, dict):
        if obj.get("type") == "reasoning" and obj.get("encrypted_content"):
            out.append(obj["encrypted_content"])
        for v in obj.values(): collect(v, out)
    elif isinstance(obj, list):
        for v in obj: collect(v, out)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        encs = []; collect(body.get("input"), encs)
        foreign = [e for e in encs if h(e) not in issued]
        n_reason = sum(1 for i in body.get("input", []) if isinstance(i, dict) and i.get("type") == "reasoning")
        print(f"[{NAME}] in_items={len(body.get('input', []))} reasoning={n_reason} foreign={len(foreign)} stream={body.get('stream')}", flush=True)
        if foreign:
            err = {"error": {"message": f"The encrypted content could not be verified. Reason: Encrypted content organization_id did not match the target organization (backend {NAME}).",
                             "type": "invalid_request_error", "param": None, "code": "invalid_encrypted_content"}}
            return self._send(400, json.dumps(err).encode())
        body["stream"] = False   # 简化：上游一律非流式
        data = json.dumps(body)
        req = AWSRequest(method="POST", url=URL, data=data, headers={"Content-Type": "application/json"})
        SigV4Auth(sess.get_credentials(), "bedrock", REGION).add_auth(req)
        r = requests.post(URL, data=data, headers=dict(req.headers), timeout=300)
        if r.status_code == 200:
            out = []; collect(r.json().get("output"), out)
            with lock: issued.update(h(e) for e in out)
        self._send(r.status_code, r.content)

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
