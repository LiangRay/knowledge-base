"""最简版：路由前无条件丢掉 input 里所有带 encrypted_content 的 reasoning item（"别人说的"方案）。"""
from litellm._logging import verbose_logger
from litellm.integrations.custom_logger import CustomLogger


class StripAllReasoning(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        inp = data.get("input")
        if isinstance(inp, list):
            kept = [i for i in inp if not (isinstance(i, dict) and i.get("type") == "reasoning")]
            if len(kept) != len(inp):
                verbose_logger.warning("[strip_all] dropped %d reasoning items", len(inp) - len(kept))
                data["input"] = kept
        return data


strip_all_instance = StripAllReasoning()
