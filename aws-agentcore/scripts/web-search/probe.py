import asyncio, json, time
from mcp import ClientSession
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

import os
URL = os.environ["AGENTCORE_GW_URL"]  # https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
CASES = [
    ("中文", "理想汽车 自研芯片 最新进展", 5),
    ("时效", "stock market news today", 5),
    ("技术", "LiteLLM prompt cache routing", 5),
    ("max25", "Amazon Bedrock AgentCore", 25),
    ("超长201", "a" * 201, 3),
]

async def main():
    async with aws_iam_streamablehttp_client(endpoint=URL, aws_region="us-east-1", aws_service="bedrock-agentcore") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for tag, q, n in CASES:
                t0 = time.time()
                try:
                    res = await s.call_tool("web-search___WebSearch", {"query": q, "maxResults": n})
                    dt = time.time() - t0
                    txt = res.content[0].text if res.content else ""
                    try:
                        items = json.loads(txt).get("results", [])
                        print(f"\n## [{tag}] {dt:.2f}s 返回{len(items)}条 isError={res.isError}")
                        for it in items[:4]:
                            print(f"- {it.get('publishedDate','-')} | {it.get('title','')[:70]} | {it.get('url','')[:90]} | text {len(it.get('text',''))}字")
                    except Exception:
                        print(f"\n## [{tag}] {dt:.2f}s isError={res.isError} RAW: {txt[:400]}")
                except Exception as e:
                    print(f"\n## [{tag}] EXC {type(e).__name__}: {str(e)[:300]}")

asyncio.run(main())
