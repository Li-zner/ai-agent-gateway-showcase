"""查询侧通用 Embedding 客户端 —— 供民法典/旅行两人格的知识库向量召回腿使用。

来源：原 app/agents/project_kb.py（求职项目知识库，2026-09-20 随求职助手下线删除）。
`_generate_embedding` 是通用函数（tools.py `_recall_two_ways` 的向量腿依赖它），
故单独成模块保留；tools.py 经 re-export 维持既有 patch/import 路径兼容。
"""

from ..core.logging import setup_logging

logger = setup_logging()


async def _generate_embedding(text: str):
    """调用 Ollama Embedding 生成向量。

    端点候选统一由 config.embedding_endpoint_candidates() 给出（显式配置优先，
    为空时从 OLLAMA_URL 派生）。2026-09-12 修复 P0：原实现把配置值当作唯一候选，
    配置为空串时请求 '' 报 "URL missing protocol" 后静默返回 None，导致
    `_recall_pg_vector` 静默返回 [] —— 向量召回整条失效且无任何指标可见，
    实测 55/55 条真实 trace 的 vector 腿 hits 全为 0。
    超时收窄到 5s：正常嵌入 <1s，给慢机留裕量即可，不该拖住整条检索。
    """
    import httpx
    from ..core.config import (
        EMBEDDING_API_KEY,
        EMBEDDING_DIMENSIONS,
        EMBEDDING_MODEL,
        EMBEDDING_PROVIDER,
        EMBEDDING_TIMEOUT,
        QWEN_API_KEY,
        embedding_endpoint_candidates,
    )
    urls = embedding_endpoint_candidates()
    if not urls:
        logger.warning("embedding 端点既未配置也无法从 OLLAMA_URL 推导，向量召回将失效")
        return None
    api_provider = EMBEDDING_PROVIDER != "ollama"
    api_key = EMBEDDING_API_KEY or (QWEN_API_KEY if api_provider else "")
    for url in urls:
        try:
            # 2026-09-30 校准修复（A3）：5s 硬编码 → 配置化（EMBEDDING_TIMEOUT，
            # 默认 10s）——ollama 冷启动 10-30s 原先必超时降级
            async with httpx.AsyncClient(timeout=httpx.Timeout(EMBEDDING_TIMEOUT)) as client:
                if api_provider:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": EMBEDDING_MODEL,
                            "input": [text[:512]],
                            "dimensions": EMBEDDING_DIMENSIONS,
                            "encoding_format": "float",
                        },
                    )
                else:
                    resp = await client.post(
                        url,
                        json={"model": EMBEDDING_MODEL, "prompt": text[:512]}
                    )
                resp.raise_for_status()
                data = resp.json()
                if api_provider:
                    items = data.get("data") or []
                    embedding = items[0].get("embedding") if items else None
                else:
                    embedding = data.get("embedding")
                # 空列表与 None 同义：向量腿没有可用输入，必须上报故障，
                # 不能让它伪装成健康腿的正常零命中。
                return embedding if isinstance(embedding, list) and embedding else None
        except Exception as _emb_err:
            logger.warning(f"embedding 端点失败（尝试下一候选）: {url} → {_emb_err}")
            continue
    logger.warning(f"{len(urls)} 个 embedding 端点全部失败，本轮向量召回失效")
    return None
