import json
import re
import asyncio
import httpx
from ..core.logging import setup_logging
from ..core.config import LLM_PRIMARY_MODEL, HTTP_TIMEOUT_MEDIUM
from ..core.jfast import loads as jloads

logger = setup_logging()


def _extract_json(text: str) -> dict:
    """
    从 LLM 回复中提取 JSON。
    处理以下情况：
    1. 纯 JSON
    2. 包裹在 ```json ... ``` markdown 代码块中
    3. 包裹在 ``` ... ``` 代码块中
    4. 前后有额外文字
    """
    if not text:
        raise json.JSONDecodeError("空响应", text, 0)

    # 尝试直接解析
    try:
        return jloads(text)
    except json.JSONDecodeError:
        logger.debug("JSON 直接解析失败，尝试 markdown 代码块提取")

    # 尝试提取 markdown 代码块
    block_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if block_match:
        try:
            return jloads(block_match.group(1).strip())
        except json.JSONDecodeError:
            logger.debug("代码块解析失败，尝试首尾大括号提取")

    # 尝试提取第一个 { } 包裹的内容
    brace_match = re.search(r'\{.*\}', text, re.DOTALL)
    if brace_match:
        try:
            return jloads(brace_match.group(0))
        except json.JSONDecodeError:
            logger.debug("大括号提取失败，尝试修复未引用的 key 后重解")

    # 最后尝试：修复常见问题（未引用的 key）。正则锚定 { 或 , 之后的键位，
    # 不再全局扫 "word:"——旧写法会把字符串值里的 URL（http://）撕成 "http"://
    fixed = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', text)
    try:
        return jloads(fixed)
    except (json.JSONDecodeError, TypeError, ValueError):  # P2 #23：覆盖 jloads 可能的异常类型
        raise



SUB_AGENT_PROMPTS = {
    "query_hotel": '''你是酒店推荐专家。只返回JSON。

【输出规则】
- 数量按热度：热门城市 3-5 家，冷门 2-3 家；至少 2 家
- 覆盖不同价位（经济/舒适/高档）

【输出格式】
{"hotels": [{"name": "名称", "price": 价格, "rating": 评分, "address": "地址", "comment": "简短评价"}]}
【约束】只输出JSON。''',

    "query_route": '''你是路线规划专家。根据出发地和目的地规划路线，只返回JSON。

【核心规则】
- 同一城市（<30km）：公交/地铁/打车
- 跨城较近（30-200km）：高铁/动车/大巴，优先高铁
- 跨城较远（>200km）：高铁/飞机，优先高铁
- 仅用户明确说自驾才用驾车
- 用户没给出发地时默认当前位置

【输出格式】
{"route": {"distance_km": 数字, "duration_min": 数字, "mode": "高铁"}}
- mode：高铁/动车/公交/驾车/飞机/大巴
【约束】只输出JSON。''',

    "query_food": '''你是美食推荐专家。只返回JSON。

【输出规则】
- 数量按热度：热门城市 3-5 家，冷门 2-3 家；至少 2 家
- 优先本地特色/必吃

【输出格式】
{"restaurants": [{"name": "名称", "cuisine": "菜系", "avg_price": 价格, "rating": 评分, "feature": "特色推荐"}]}
【约束】只输出JSON。''',
}

async def call_sub_agent(agent_name: str, args: dict, user_query: str, retry: bool = True,
                         username: str = ""):
    """调用子Agent，如果返回非法JSON则自动重试一次

    username 供扣费（2026-09-09 主人拍板：内部 LLM 调用计入计费）。
    """
    # 与主链路同款取 Key 逻辑（统一走 chat_support，杜绝多 Key 白名单不一致的 400）
    from ..services.chat_support import get_deepseek_key
    api_key = await get_deepseek_key()
    if not api_key:
        return {"error": "Missing DeepSeek Key"}

    system_prompt = SUB_AGENT_PROMPTS.get(agent_name, "")
    if not system_prompt:
        return {"error": f"Unknown agent: {agent_name}"}
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"用户问题：{user_query}\n参数：{json.dumps(args)}"}
    ]
    
    async def _call():
        # 主力模型失败自动 flash 兜底（复用主链路原语 post_chat_completion：
        # 端点按模型路由 + Key 白名单一致 + qwen 温度剥离；2026-09-10 实测单发
        # 主模型时上游一抖，酒店/路线/美食三个子 Agent 同时报错全军覆没）
        from ..services.chat_support import post_chat_completion
        from ..core.concurrency import llm_semaphore
        # 纳入全局 LLM 并发闸（2026-09-10 审查 P2：子 Agent 被 react_steps gather
        # 并行最多 4 路，原先可瞬间占满 LLM 并发额度）
        async with llm_semaphore:
            ok, data = await post_chat_completion(
                {"model": LLM_PRIMARY_MODEL, "messages": messages}, HTTP_TIMEOUT_MEDIUM)
        if not ok:
            # 统一抛 HTTPError 进既有的退避重试/兜底通道（诊断体已截断，防 Key 入日志）
            raise httpx.HTTPError(f"upstream {data.get('status')}: {str(data.get('body'))[:80]}")

        content = data["choices"][0]["message"]["content"]

        # ---- Token 精细计量（标签跟随实际应答模型：flash 兜底时不再误挂主模型名）----
        from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
        used_model = data.get("model", LLM_PRIMARY_MODEL)
        usage = data.get("usage", {})
        prompt_tk = usage.get('prompt_tokens', 0) or 0
        completion_tk = usage.get('completion_tokens', 0) or 0
        llm_tokens_total.labels(type='input').inc(prompt_tk)
        llm_tokens_total.labels(type='output').inc(completion_tk)
        llm_tokens_detail.labels(model=used_model, endpoint='sub_agent', type='input').inc(prompt_tk)
        llm_tokens_detail.labels(model=used_model, endpoint='sub_agent', type='output').inc(completion_tk)
        llm_requests_total.labels(model=used_model, endpoint='sub_agent', status='success').inc()
        # 计入计费（2026-09-09 主人拍板）：指标已打点，复用不含指标的扣费入口
        if username:
            from ..services.llm_streaming import bill_token_usage
            bill_token_usage(usage, username, "",
                             remark=f"子Agent[{agent_name}]消耗 {prompt_tk + completion_tk} tokens")

        return _extract_json(content)
    
    try:
        return await _call()
    except (json.JSONDecodeError, TypeError, ValueError, KeyError, IndexError,
            httpx.HTTPError) as e:
        # KeyError/IndexError 一并进重试（2026-09-14 审计 P1）：上游返回结构缺
        # choices/message 时 data["choices"][0]["message"] 会抛，原捕获列表未含，
        # 单次畸形响应直接打穿调用方；httpx.HTTPError：网络抖动/限流首调失败直接重试
        if retry:
            # 重试前加退避（P1 #34：防 API 限流时立即重试加剧压力）
            await asyncio.sleep(0.5)
            logger.warning(f"子Agent {agent_name} 首次调用失败，自动重试")
            messages[0]["content"] = system_prompt + "\n【重要】只输出纯JSON，不要添加任何解释。"
            try:
                return await _call()
            except (json.JSONDecodeError, TypeError, ValueError, KeyError, IndexError,
                    httpx.HTTPError) as e2:
                # 日志截断：避免记录可能含 API Key 的完整异常（P1 #5）
                logger.error(f"子Agent {agent_name} 重试仍失败: {str(e2)[:120]}")
                return {"error": f"重试失败: {str(e2)[:120]}"}
        return {"error": f"JSON解析失败: {str(e)[:120]}"}