# AgentCore Web Search 测试脚本

```bash
python3 -m venv ~/.venvs/agentcore-ws && ~/.venvs/agentcore-ws/bin/pip install boto3 mcp-proxy-for-aws strands-agents
export AGENTCORE_GW_URL="https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
```

| 脚本 | 用途 |
|---|---|
| `api_search.py [region]` | InvokeWebSearch API：基础 / 域名 include / exclude / 最近 7 天 |
| `raw_search.py "<query>" [n]` | 经 Gateway 直接调 MCP 工具（`aws_iam_streamablehttp_client`） |
| `probe.py` | Gateway 多场景探测（中文、时效、文档、25 条、201 字符超限） |
| `agent_search.py` | Strands Agent + Bedrock 模型 + MCPClient |
