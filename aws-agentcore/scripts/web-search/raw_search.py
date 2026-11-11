import asyncio, json, sys, time
from mcp import ClientSession
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

import os
URL = os.environ["AGENTCORE_GW_URL"]  # https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp

async def main(query, n):
    async with aws_iam_streamablehttp_client(endpoint=URL, aws_region="us-east-1", aws_service="bedrock-agentcore") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            for t in tools.tools:
                print("TOOL:", t.name)
                print(json.dumps(t.inputSchema, ensure_ascii=False, indent=1)[:1500])
            t0 = time.time()
            res = await s.call_tool(tools.tools[0].name, {"query": query, "maxResults": n})
            print(f"\nLATENCY: {time.time()-t0:.2f}s  isError={res.isError}")
            for c in res.content:
                print(getattr(c, "text", c)[:4000])

asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5))
