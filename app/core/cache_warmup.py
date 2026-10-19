"""
语义缓存预热模块：启动时写入有标准答案的固定问答。

修复（P2）：旧实现会给 20 个无答案的热门查询写"正在为您查询…请稍候..."占位，
并声称"首次访问时会被真实结果覆盖"——该机制不存在（缓存命中不会触发生成），
占位会被原样当答案返回；且写入用 cache_ctx=""，运行时永远按 build_cache_ctx
（人格|位置|画像指纹）查询，这些条目实际永远不可达（纯死写）。现仅预热有
标准答案的条目。

2026-09-22 审阅 P2：预热键再对齐一次运行时维度（persona/lang/model），并把
"能命中到什么"的边界写在下面，不再声称"保证可命中"。
"""
from .semantic_cache import SemanticCache
from .config import LLM_PRIMARY_MODEL
from .logging import setup_logging

logger = setup_logging()

# 高频查询列表（硬编码，来自实际使用统计）

# 无需调用工具，直接有标准答案的查询
_HOT_ANSWERS = {
    "离婚冷静期是多久": "根据《中华人民共和国民法典》第一千零七十七条规定，自婚姻登记机关收到离婚登记申请之日起三十日内，任何一方不愿意离婚的，可以向婚姻登记机关撤回离婚登记申请。前款规定期限届满后三十日内，双方应当亲自到婚姻登记机关申请发给离婚证；未申请的，视为撤回离婚登记申请。这就是俗称的「离婚冷静期」，为期30天。",
}

# 预热条目所处的上下文维度：与 chat_stream_ctx.build_stream_ctx 生成键时用的
# "无显式选择"默认值一一对应——人格走默认的统一助手、请求未带 lang 时取空串、
# 模型取 persona.model 缺省的 LLM_PRIMARY_MODEL（真实选中的模型要等人格解析完才
# 知道，预热期拿不到，只能按默认人格的那一条）。
_DEFAULT_PERSONA_ID = "unified"
_DEFAULT_LANG = ""


async def warmup_semantic_cache():
    """启动预热：只写入有标准答案的固定问答（键构成与运行时默认路径一致）。

    ponytail: 已知天花板——真实键首位是**不可空**的 username 指纹（Bug #1 的
    跨用户隔离），所以这里写出的条目只与"匿名/无 username"的上下文同键，已登录
    用户不会命中。要让一条标准答案真正跨用户复用，需要在 semantic_cache 里开一个
    显式 public 分区并在读取侧带第二优先级（涉及跨用户读取语义，属产品决策），
    不是本模块能自己收口的；届时把 _DEFAULT_* 换成该分区的固定分区键即可。
    """
    logger.info("开始预热语义缓存...")
    warmed = 0
    for query, answer in _HOT_ANSWERS.items():
        cache_ctx = SemanticCache.build_cache_ctx(
            "", _DEFAULT_PERSONA_ID, "", "",
            user_query=query, lang=_DEFAULT_LANG, model=LLM_PRIMARY_MODEL,
        )
        try:
            await SemanticCache.set(query, answer, cache_ctx=cache_ctx)
            warmed += 1
            logger.info(f"  预热缓存: {query[:30]}")
        except Exception as e:
            logger.warning(f"  预热失败: {query[:30]} - {e}")
    logger.info(f"语义缓存预热完成，共 {warmed}/{len(_HOT_ANSWERS)} 条固定答案")
