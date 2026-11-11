import sys, time
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

URL = "https://bedrock-search-gw-v2tagaojws.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "us.anthropic.claude-haiku-4-5-20251001-v1:0"

mcp = MCPClient(lambda: aws_iam_streamablehttp_client(endpoint=URL, aws_region="us-east-1", aws_service="bedrock-agentcore"))
model = BedrockModel(model_id=MODEL, region_name="us-east-1", max_tokens=4096)

with mcp:
    agent = Agent(model=model, tools=mcp.list_tools_sync(), callback_handler=None,
                  system_prompt="今天是 2026-11-11。你是研究助手，必须用 web search 获取最新信息，用中文回答，每个关键事实后标注来源 URL。")
    t0 = time.time()
    r = agent(sys.argv[1])
    print(str(r))
    m = r.metrics
    print(f"\n---\nMODEL={MODEL} 总耗时 {time.time()-t0:.1f}s | cycles={m.cycle_count} | tool调用={ {k: v.call_count for k, v in m.tool_metrics.items()} } | tokens={m.accumulated_usage}")
