#!/usr/bin/env python3
"""Kiro API Key 并发测试脚本
测试同一个 API Key 能支持多少路并发请求。
"""
import asyncio
import time
import os

KIRO_CLI = "/root/.local/bin/kiro-cli"
os.environ["KIRO_API_KEY"] = "ksk_MiFC*"


async def kiro_chat(session_num: int):
    """发起单个 kiro-cli chat 请求"""
    start = time.time()
    prompt = f"Reply with exactly: 'Session {session_num} OK'"

    try:
        proc = await asyncio.create_subprocess_exec(
            KIRO_CLI, "chat", "--no-interactive", "-a", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            cwd="/tmp"
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        elapsed = time.time() - start
        output = stdout.decode().strip()
        err = stderr.decode().strip()

        if proc.returncode == 0 and output:
            return {"session": session_num, "status": "OK", "response": output[:100], "duration": f"{elapsed:.1f}s"}
        else:
            return {"session": session_num, "status": "FAIL", "error": (err or output)[:150], "duration": f"{elapsed:.1f}s"}

    except asyncio.TimeoutError:
        try:
            proc.kill()
        except:
            pass
        return {"session": session_num, "status": "TIMEOUT", "duration": f"{time.time()-start:.1f}s"}
    except Exception as e:
        return {"session": session_num, "status": "ERROR", "error": str(e), "duration": f"{time.time()-start:.1f}s"}


async def run_test(levels=[2, 3, 5, 10, 15, 20]):
    """逐级加压测试"""
    for level in levels:
        print(f"\n{'='*60}")
        print(f"🚀 并发测试: {level} 个同时请求 (同一 API Key)")
        print(f"{'='*60}")

        start = time.time()
        tasks = [kiro_chat(i + 1) for i in range(level)]
        results = await asyncio.gather(*tasks)
        total_time = time.time() - start

        successes = [r for r in results if r["status"] == "OK"]
        failures = [r for r in results if r["status"] != "OK"]

        print(f"\n  ✅ 成功: {len(successes)}/{level} | 总耗时: {total_time:.1f}s")
        if failures:
            print(f"  ❌ 失败: {len(failures)}/{level}")
            for r in failures[:5]:
                print(f"      Session {r['session']}: {r['status']} | {r.get('error', '')[:80]}")

        ok_times = [float(r["duration"].rstrip("s")) for r in successes]
        if ok_times:
            print(f"  ⏱️  响应时间: min={min(ok_times):.1f}s, max={max(ok_times):.1f}s, avg={sum(ok_times)/len(ok_times):.1f}s")

        if level != levels[-1]:
            wait = 10
            print(f"\n  ⏳ 等待 {wait}s 再测下一级...")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(run_test())
