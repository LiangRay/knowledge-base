import boto3, time, json, sys
from datetime import datetime, timezone, timedelta

REGION = sys.argv[1] if len(sys.argv) > 1 else "us-west-2"
c = boto3.client("bedrock-agentcore", region_name=REGION)

def run(tag, **kw):
    t0 = time.time()
    try:
        r = c.invoke_web_search(webSearchId="web-search.v1", **kw)
        res = r["results"]
        print(f"\n## [{tag}] {time.time()-t0:.2f}s 返回{len(res)}条")
        for it in res[:5]:
            print(f"- {it.get('publishedAt','-')} | {it.get('title','')[:60]} | {it.get('url','')[:85]}")
    except Exception as e:
        print(f"\n## [{tag}] {time.time()-t0:.2f}s ERR {type(e).__name__}: {str(e)[:300]}")

run("基础", searchQuery="Amazon Bedrock AgentCore Web Search API", maxResults=5)
run("域名include", searchQuery="AgentCore", maxResults=5,
    filters={"domainFilter": {"include": ["aws.amazon.com"]}})
run("域名exclude", searchQuery="AgentCore", maxResults=5,
    filters={"domainFilter": {"exclude": ["aws.amazon.com", "docs.aws.amazon.com"]}})
run("最近7天", searchQuery="OpenAI", maxResults=5,
    filters={"publishedDateFilter": {"publishedAfter": datetime.now(timezone.utc) - timedelta(days=7)}})
