"""LLM spot-check for anonymized frames via Amazon Bedrock Converse API.
Asks a multimodal model whether any face / license plate is still identifiable.
Usage: python llm_qa.py [--model MODEL_ID] [--region REGION] img1_anon.jpg ...
Output: one JSON line per image (verdict, findings, tokens, latency).
"""
import argparse, json, re, time
import boto3

PROMPT = """你是数据脱敏质检员。这张图已经对人脸和车牌做过模糊处理。
请检查图中是否还有【未被模糊、仍可辨认或部分可辨认】的：
1) 人脸（包括侧脸、被画面边缘截断的半张脸、远处小脸）
2) 车牌（包括残缺、倾斜、远处的车牌）
3) 其他可识别个人的信息（电话号码、姓名、屏幕内容等）
已经模糊的区域不算。宁可多报不要漏报。
只输出 JSON，不要其他文字：
{"pass": true/false, "findings": [{"type": "face|plate|other", "location": "在图中的大致位置", "detail": "简述"}]}
无遗漏时 pass=true、findings=[]。"""


def check(client, model, path):
    with open(path, "rb") as f:
        img = f.read()
    fmt = "png" if path.lower().endswith(".png") else "jpeg"
    t0 = time.perf_counter()
    r = client.converse(
        modelId=model,
        messages=[{"role": "user", "content": [
            {"image": {"format": fmt, "source": {"bytes": img}}},
            {"text": PROMPT}]}],
        inferenceConfig={"maxTokens": 4000},  # 新模型不接受 temperature
    )
    dt = time.perf_counter() - t0
    # 新模型可能先返回 reasoningContent 块，只拼接 text 块
    text = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
    m = re.search(r"\{.*\}", text, re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {"raw": text}
    except json.JSONDecodeError:
        verdict = {"raw": text}
    u = r["usage"]
    return {"image": path, "model": model, "latency_s": round(dt, 1),
            "in_tokens": u["inputTokens"], "out_tokens": u["outputTokens"], **verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="global.anthropic.claude-haiku-5-5")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("images", nargs="+")
    a = ap.parse_args()
    client = boto3.client("bedrock-runtime", region_name=a.region)
    for p in a.images:
        print(json.dumps(check(client, a.model, p), ensure_ascii=False))


if __name__ == "__main__":
    main()
