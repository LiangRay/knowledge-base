"""
encrypted_reasoning_guard —— LiteLLM 自定义 hook：
"软亲和 + 换 deployment 时丢弃加密 reasoning"

背景
----
GPT 推理模型（Responses API，store=false）每轮都会回传
{"type":"reasoning","encrypted_content":"..."}。客户端（如 Codex）下一轮会原样带回。
这段密文只有"同一个加密边界"的后端才解得开（OpenAI 是 org 级别，Azure 是 resource 级别）。
多供应商负载均衡时，如果下一轮被路由到别的后端，就会报 400 invalid_encrypted_content，
有的上游会报 500。

LiteLLM 自带的 encrypted_content_affinity 是"硬亲和"：原 deployment 不可用时直接
返回 429/503，不会切到其他 deployment。

本 hook 的做法
--------------
1. async_filter_deployments（选路前）
   - 打开 LiteLLM 内置的输出打标开关 litellm_metadata["encrypted_content_affinity_enabled"]，
     让响应里的 reasoning item 被编码进 origin model_id
     （encitem_... / litellm_enc:...，含流式事件）。
   - 解析 input 里的 origin model_id：
     - origin 健康 → 只返回 origin（软亲和，保住推理连续性和 prompt cache）
     - origin 不可用 → 返回全部健康 deployment（不报错，允许故障切换）
2. async_pre_call_deployment_hook（deployment 已选定、发出请求前；每次 retry/fallback 都会调用）
   - 比较 input 里每个 reasoning item 的 origin 与当前选中的 deployment id。
     不一致的就丢掉整个 reasoning item；一致的保留，交给 LiteLLM 自己解包后发给上游。
   - 无法判断来源的 reasoning item（没有打标的历史数据）按 UNTAGGED_POLICY 处理。

只处理 Responses API（aresponses），其余调用直接透传。
任何异常都只记日志，不阻断请求。
"""
from __future__ import annotations

import copy
import os
from typing import Any

from litellm._logging import verbose_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.responses.utils import ResponsesAPIRequestUtils as _U

# keep  : 没打标的 reasoning 原样放行（默认，兼容开启 hook 前的历史会话）
# strip : 没打标的 reasoning 也丢弃（最保守，等于"永远不跨后端回放"）
UNTAGGED_POLICY = os.getenv("ERG_UNTAGGED_POLICY", "keep")
# 只对这些 model_name 生效，逗号分隔；为空表示全部生效
ONLY_GROUPS = {g.strip() for g in os.getenv("ERG_MODEL_GROUPS", "").split(",") if g.strip()}
TAG = "[encrypted_reasoning_guard]"


def _origin_of(item: dict) -> str | None:
    """从 LiteLLM 打标的 item id / encrypted_content 中解出 origin model_id。"""
    iid = item.get("id")
    if isinstance(iid, str):
        dec = _U._decode_encrypted_item_id(iid)
        if dec and dec.get("model_id"):
            return dec["model_id"]
    enc = item.get("encrypted_content")
    if isinstance(enc, str):
        mid, _ = _U._unwrap_encrypted_content_with_model_id(enc)
        if mid:
            return mid
    return None


def _is_enc_reasoning(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "reasoning" and bool(item.get("encrypted_content"))


class EncryptedReasoningGuard(CustomLogger):
    # ---------- 1. 选路前：软亲和 + 打开输出打标 ----------
    async def async_filter_deployments(self, model, healthy_deployments, messages,
                                       request_kwargs=None, parent_otel_span=None):
        try:
            if ONLY_GROUPS and model not in ONLY_GROUPS:
                return healthy_deployments
            rk = request_kwargs or {}
            if "litellm_metadata" not in rk:          # 只有 Responses API 走这个分支
                return healthy_deployments
            rk["litellm_metadata"]["encrypted_content_affinity_enabled"] = True
            rk["litellm_metadata"]["erg_enabled"] = True

            inp = rk.get("input")
            if not isinstance(inp, list):
                return healthy_deployments
            # 以最后一个打标的 reasoning 的 origin 为准
            origin = None
            for it in inp:
                if _is_enc_reasoning(it):
                    origin = _origin_of(it) or origin
            if not origin:
                return healthy_deployments
            pinned = [d for d in healthy_deployments
                      if (d.get("model_info") or {}).get("id") == origin]
            if pinned:
                verbose_logger.info("%s soft-pin -> %s", TAG, origin)
                return pinned
            verbose_logger.warning("%s origin %s 不在健康列表，放开路由，交由 deployment hook 剥离",
                                   TAG, origin)
            return healthy_deployments
        except Exception as e:  # 失败开放
            verbose_logger.exception("%s filter error: %s", TAG, e)
            return healthy_deployments

    # ---------- 2. deployment 已选定：不一致就剥离 ----------
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        try:
            ct = getattr(call_type, "value", call_type)
            if ct not in ("aresponses", "responses"):
                return None
            md = kwargs.get("litellm_metadata") or {}
            if not md.get("erg_enabled"):
                return None
            inp = kwargs.get("input")
            if not isinstance(inp, list) or not any(_is_enc_reasoning(i) for i in inp):
                return None
            target = (md.get("model_info") or {}).get("id")
            if not target:
                return None

            kept, dropped = [], 0
            for it in inp:
                if _is_enc_reasoning(it):
                    o = _origin_of(it)
                    if (o is None and UNTAGGED_POLICY == "strip") or (o is not None and o != target):
                        dropped += 1
                        continue
                kept.append(it)
            if not dropped:
                return None
            verbose_logger.warning("%s target=%s 剥离 %d 个跨后端 reasoning item（保留 %d 个 item）",
                                   TAG, target, dropped, len(kept))
            new_kwargs = dict(kwargs)
            new_kwargs["input"] = copy.copy(kept)
            return new_kwargs
        except Exception as e:
            verbose_logger.exception("%s deployment hook error: %s", TAG, e)
            return None


encrypted_reasoning_guard_instance = EncryptedReasoningGuard()
