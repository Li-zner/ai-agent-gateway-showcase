"""
配置管理（Pydantic BaseSettings，2026-08-18 迁移，遗留 A2）

- 统一从环境变量 + .env 读取，类型自动校验（int/float/str），非法值启动即报错。
- 安全凭据缺失 fail loudly（SESSION_SECRET_KEY / JWT_SECRET / ADMIN_PASSWORD）。
- CORS 通配符与 allow_credentials=True 冲突时拒绝启动。
"""
import ipaddress
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


# 权威 .env 位于仓库上一级（D:\桌面\.env，2026-09-06 起用户指定桌面直放）。
# 容器内该路径不存在也无妨：环境变量由 compose env_file 注入 os.environ，
# 此处仅为本地直跑 python/脚本时的兜底读取。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DESKTOP_ENV = os.path.join(_REPO_ROOT, "..", ".env")


class _Settings(BaseSettings):
    """全量配置（字段名小写，自动匹配 UPPER_SNAKE 环境变量，大小写不敏感）"""
    model_config = SettingsConfigDict(
        env_file=_DESKTOP_ENV,
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 安全过滤 / 地点提取 ----------
    safe_filter_message: str = "该问题涉及敏感信息，小助手不便回答哦~"
    # 答案级本地化：界面语言为 en 时吐出这条英文安全提示（中文保持默认值不变）。
    safe_filter_message_en: str = "This question involves sensitive information, so the assistant can't answer it."
    safe_filter_words_file: str = ""
    default_city: str = "梧州"

    # ---------- 高德地图 ----------
    # 天气/IP 定位/地理编码共用 Key（原 map_api 四处裸 os.getenv，2026-09-10 审查收编）
    amap_api_key: str = ""

    # ---------- Cloudflare Turnstile（A-P1-4：匿名发码人机校验） ----------
    # site_key 公开（前端渲染挂件）、secret 服务端校验；**两者留空 = 功能关闭**，
    # 本地开发与未配置部署完全不受影响（2026-09-29 接入，缺省安全）
    turnstile_site_key: str = ""
    turnstile_secret: str = ""

    # ---------- 可观测性 ----------
    # Prometheus /metrics 与 /test-otel 的 Bearer 门禁（原 observability 裸
    # os.getenv，2026-09-10 审查收编；未配置即 fail-closed 403）
    metrics_token: str = ""

    # ---------- 运行环境 ----------
    # 测试环境标记（原裸 os.getenv("APP_ENV")，与 BaseSettings 双轨，2026-09-07 审查收编）
    app_env: str = ""

    # 可信反代开关（RT-1，2026-09-19 审查）：仅当部署链路确为
    # Cloudflare Tunnel / nginx 反代时才置 1，_client_ip 才读转发头；
    # 默认 0 = 只信 socket 对端地址（防伪造头绕登录锁/短信限额/oauth 限频）
    trust_proxy_headers: bool = False
    # 可信反代网段（2026-09-22 审查 P1）：只有 socket 对端精确命中这里的 CIDR，
    # _client_ip 才采信 CF-Connecting-IP / X-Forwarded-For。
    # 默认只给回环：原实现把整个 RFC1918 写死成可信，而 docker 网桥上的直连源
    # 本身就是 172.18.0.x——任何能直连网关端口的容器都能自报客户端 IP，把自己的
    # 登录失败锁/短信日限额/oauth 限频甩给无辜 IP。生产按实际反代网段精确配置。
    trusted_proxy_cidrs: str = "127.0.0.1/32"

    # ---------- 数据库 ----------
    database_url: str = ""
    # 中控台「问数」只读观测专用 DSN（账号见 scripts/rag_readonly_role.sql）。
    # 不回退 database_url：只读观测一旦被赋予主账号，SQL 守卫就是纸糊的。
    rag_readonly_dsn: str = ""
    # 中控台「DB Agent」双通道 DSN（dbagent_readonly / dbagent_writer 角色，
    # 建号脚本见 DBagent 项目 scripts/readonly_role.sql）。同样不回退主账号。
    dbagent_readonly_dsn: str = ""
    dbagent_write_dsn: str = ""
    # DB Agent 影子期开关（env DBAGENT_PROPOSE_ONLY=1）：写门批准后不执行，
    # 只把 SQL+参数 登记改动日志（action='proposed'）交人工执行。
    # 单任务可用提交体的 propose_only 覆盖（评测/演练用）。
    dbagent_propose_only: bool = False
    # DB Agent 并发任务上限（跨实例按 PG 活跃行数计，P2-1 配置化）：
    # 多实例部署时各副本必须配同值——闸在库里数的是全集群活跃任务。
    dbagent_max_active: int = 3
    # 每提交者并发任务上限（P2-3）：防单人占满全局槽；同款跨实例口径
    dbagent_per_user_active: int = 2
    # 单任务 token 预算（P2-3，input+output 累计）：超限转 failed 留痕。
    # 实测最重任务（分节分析）约 44k，400k≈10 倍余量——只拦失控循环
    dbagent_task_token_budget: int = 400000
    timeout_seconds: float = 30.0

    # ---------- GitHub OAuth ----------
    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret_key: str = ""

    # ---------- JWT ----------
    jwt_secret: str = ""
    jwt_secret_old: str = ""
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 30
    refresh_max_days: int = 30

    # ---------- LLM 槽位（2026-10-08 待拍板4：按角色命名，不再绑供应商名）----------
    # primary=主力（qwen 系自动路由百炼端点）、fallback=降级（DeepSeek 官方 flash 档）、
    # last_resort=DB Agent 末级兜底。环境变量同名（LLM_PRIMARY_MODEL / LLM_FALLBACK_MODEL /
    # LLM_LAST_RESORT_MODEL）；旧键 DEEPSEEK_MODEL / DEEPSEEK_FLASH_MODEL 已废弃——
    # 主力一直是 qwen3.7-flash，键名绑 deepseek 会误导，.env 需同步改键名。
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_api_timeout: float = 30.0
    llm_primary_model: str = "qwen3.7-flash"
    llm_fallback_model: str = "deepseek-flash"
    # DB Agent 跨供应商兜底（P1-1）：主通道（flash+主模型）两轮全挂后点名的
    # 最后一级。与主模型同属百炼——百炼整体宕机时三级全挂（ponytail 天花板，
    # 升级 = 兜底换 DeepSeek 官方非 flash 档模型）。
    # 10-08 主人规则「谁便宜用谁」：官方价目（华北2 原价，输入/输出 元每百万 token）
    # qwen3.7-flash 0.2/0.8、qwen3.8-flash 0.8/2.7，末级换成 3.7 后阶梯末两级同名，
    # 语义变成「同端点重试两轮」；跨供应商冗余不变（3.8 与 3.7 本来就同属百炼）。
    llm_last_resort_model: str = "qwen3.7-flash"
    deepseek_fallback_message: str = "服务器繁忙，请稍后再提问吧！"
    # 答案级本地化：界面语言为 en 时这条兜底也走英文（与 safe_filter_message_en 同口径）。
    deepseek_fallback_message_en: str = "The server is busy, please try again later."
    # 其余答案级中文兜底/标记，同样配中英两版（A 方案全量本地化的补齐档）。
    empty_answer_message: str = "抱歉，我暂时无法回答。"
    empty_answer_message_en: str = "Sorry, I can't answer this right now."
    incomplete_answer_message: str = "抱歉，我暂时无法完成完整的回答。"
    incomplete_answer_message_en: str = "Sorry, I couldn't finish a complete answer."
    content_filtered_note: str = "[内容已过滤]"
    content_filtered_note_en: str = "[content filtered]"
    llm_enable_thinking: bool = False

    # ---------- Qwen（阿里云百炼 OpenAI 兼容端点；主力模型为 qwen 系时使用）----------
    qwen_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_api_key: str = ""
    rerank_sim_threshold: float = 0.6
    llm_temperature: float = 0.3

    # ---------- 对话管理 ----------
    # summary_threshold 是历史读取上限（get_context(limit=…)），不是压缩阈值：
    # 压缩在 20 条处折叠（runner max_messages=20 / chat_stream_ctx 兜底 20，
    # 2026-09-22 拍板），读取留 10 条余量。旧注释"保留15轮"口径已废（09-30 对齐，
    # 生效值以 /api/admin/rag/effective-config 为准）。
    summary_threshold: int = 30
    history_limit: int = 30
    history_ttl: int = 86400
    history_summary_ttl: int = 604800
    rag_answer_top_k: int = 10

    # ---------- 能力开关（2026-09-30"建了不闭环"治理） ----------
    # LLM 二级意图分类与多 Agent 交叉审阅此前"实现了但调用点写死关"——现挂环境
    # 变量（INTENT_USE_LLM / ORCH_ENABLE_PHASE2），默认关、行为与历史等价，
    # 可被真实触发；生效值随 /api/admin/rag/effective-config 报出。
    intent_use_llm: bool = False
    orch_enable_phase2: bool = False

    # ---------- 超时 ----------
    # 2026-09-30 校准修复（逻辑审阅 A2）：单次子 Agent 调用最坏 = 主模型 15s +
    # flash 兜底 15s（post_chat_completion 模型级轮换，已核无 Key 级放大），
    # 30s 帽子刚好卡在掐掉合法最坏的位置。45s 的余量给兜底模型跑完
    # （主人拍板"45s 在兜底模型加时长"）；带 prompt 修复的整体重试（~60s）
    # 仍会被掐，接受——重试被掐 = 降级为工具错误，可容忍。
    tool_timeout: float = 45.0
    http_timeout_short: float = 5.0
    http_timeout_medium: float = 15.0
    http_timeout_long: float = 30.0
    map_api_timeout: float = 8.0
    db_acquire_timeout: float = 10.0
    db_command_timeout: float = 30.0
    task_timeout: float = 120.0
    llm_connect_timeout: float = 10.0
    task_ttl_seconds: int = 3600

    # ---------- Embedding / 语义缓存 ----------
    embedding_api_url: str = "http://localhost:11434/api/embeddings"
    embedding_api_key: str = ""
    embedding_model: str = "shaw/dmeta-embedding-zh"
    embedding_provider: str = "ollama"
    embedding_dimensions: int = 768
    # 2026-09-30 校准修复（逻辑审阅 A3）：原 kb_embedding 硬编码 5s 总超时，
    # ollama 冷启动 10-30s 首嵌必超时降级，且独立于 http_timeout_* 族调不动。
    # 10s 覆盖温启动；冷启动由运维先 warm。
    embedding_timeout: float = 10.0
    cache_similarity_threshold: float = 0.85
    cache_l0_ttl: int = 600

    # ---------- 限流（角色分级）----------
    daily_request_limit: int = 50000
    daily_token_limit: int = 10000000
    second_request_limit: int = 500
    admin_qps_limit: int = 5000
    admin_daily_req: int = 1000000
    admin_daily_token: int = 50000000
    admin_concurrent: int = 500
    user_qps_limit: int = 2000
    user_daily_req: int = 500000
    user_daily_token: int = 50000000
    user_concurrent: int = 200

    # ---------- GitHub 试用额度 ----------
    github_question_limit: int = 20

    # ---------- 短信 ----------
    alibaba_cloud_access_key_id: str = ""
    alibaba_cloud_access_key_secret: str = ""
    sms_sign: str = "旅行助手"
    sms_template_code: str = "SMS_000000"
    sms_code_expire_seconds: int = 300
    sms_send_min_interval: int = 60
    sms_send_hour_limit: int = 5
    sms_attempt_limit: int = 5
    sms_attempt_lock_seconds: int = 900

    # ---------- 安全凭据 ----------
    admin_phone: str = ""
    admin_username: str = "admin"
    admin_password: str = ""

    # ---------- CORS / 回调 ----------
    # 2026-09-12：入口端口迁移 100xx→1019x（WinNAT 保留段），新旧并留一版以防 .env 未跟上
    cors_origins: str = "http://localhost:10088,http://localhost:10086,http://localhost:10189,http://localhost:10190,http://localhost:10090"
    github_redirect_uri: str = "http://localhost:10088/auth/github/callback"
    gateway_public_url: str = "http://localhost:10088"
    # AUTH-4（2026-09-20 审查）：OAuth state 会话 cookie 的 Secure 标记。
    # 默认开（生产 HTTPS/Tunnel 链路）；仅当整套部署确认走非回环 HTTP 时才设
    # SESSION_COOKIE_SECURE=0——浏览器对 127.0.0.1 有 secure-context 豁免，
    # 本地回环验收不受影响。
    session_cookie_secure: bool = True

    # ---------- 密码 ----------
    bcrypt_max_bytes: int = 72
    # C7：SHA-256 预处理后无 72 字节硬上限，此为策略上限（防超长密码 DoS）
    password_max_bytes: int = 128

    # ---------- 支付 ----------
    payment_success_rate: float = 0.95
    payment_simulated_delay_min: float = 1.0
    payment_simulated_delay_max: float = 3.0
    recharge_min_amount: float = 0.01
    recharge_max_amount: float = 999999.00
    default_wallet_balance: float = 0.00
    order_expire_seconds: int = 600
    token_cost_rate: float = 10.0


_settings = _Settings()

# ===== 安全校验（fail loudly，A2：BaseSettings 迁移后保留）=====
if not _settings.session_secret_key:
    raise RuntimeError("SESSION_SECRET_KEY 环境变量未设置！请在 .env 中配置一个强随机字符串。")
if not _settings.jwt_secret:
    raise RuntimeError("JWT_SECRET 环境变量未设置！请在 .env 中配置一个强随机字符串。")
if not _settings.admin_password and _settings.app_env != "test":
    raise RuntimeError("ADMIN_PASSWORD 环境变量未设置！admin 账户空密码是安全漏洞，请配置强随机字符串。")

# CORS：逗号分隔转列表 + 逐项 trim；通配符与 allow_credentials=True 冲突时拒绝启动
CORS_ORIGINS = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]
if "*" in CORS_ORIGINS:
    raise RuntimeError("CORS_ORIGINS 不能包含 '*'（与 allow_credentials=True 冲突），请配置具体来源")

# ===== 模块级导出（保持所有 import 名兼容）=====
POSTGRES_DSN = _settings.database_url
DATABASE_URL = POSTGRES_DSN  # MemoryManager 使用的别名
RAG_READONLY_DSN = _settings.rag_readonly_dsn
DBAGENT_READONLY_DSN = _settings.dbagent_readonly_dsn
DBAGENT_WRITE_DSN = _settings.dbagent_write_dsn
DBAGENT_PROPOSE_ONLY = _settings.dbagent_propose_only
DBAGENT_MAX_ACTIVE = _settings.dbagent_max_active
DBAGENT_PER_USER_ACTIVE = _settings.dbagent_per_user_active
DBAGENT_TASK_TOKEN_BUDGET = _settings.dbagent_task_token_budget
TIMEOUT_SECONDS = _settings.timeout_seconds

GITHUB_CLIENT_ID = _settings.github_client_id
GITHUB_CLIENT_SECRET = _settings.github_client_secret
SESSION_SECRET_KEY = _settings.session_secret_key
SESSION_COOKIE_SECURE = _settings.session_cookie_secure

SECRET_KEY = _settings.jwt_secret
SECRET_KEY_OLD = _settings.jwt_secret_old or None
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = _settings.access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = _settings.refresh_token_expire_days
REFRESH_MAX_DAYS = _settings.refresh_max_days

DEEPSEEK_API_BASE = _settings.deepseek_api_base
DEEPSEEK_API_TIMEOUT = _settings.deepseek_api_timeout
LLM_PRIMARY_MODEL = _settings.llm_primary_model
LLM_FALLBACK_MODEL = _settings.llm_fallback_model
LLM_LAST_RESORT_MODEL = _settings.llm_last_resort_model
DEEPSEEK_FALLBACK_MESSAGE = _settings.deepseek_fallback_message
DEEPSEEK_FALLBACK_MESSAGE_EN = _settings.deepseek_fallback_message_en
EMPTY_ANSWER_MESSAGE = _settings.empty_answer_message
EMPTY_ANSWER_MESSAGE_EN = _settings.empty_answer_message_en
INCOMPLETE_ANSWER_MESSAGE = _settings.incomplete_answer_message
INCOMPLETE_ANSWER_MESSAGE_EN = _settings.incomplete_answer_message_en
CONTENT_FILTERED_NOTE = _settings.content_filtered_note
CONTENT_FILTERED_NOTE_EN = _settings.content_filtered_note_en
LLM_ENABLE_THINKING = _settings.llm_enable_thinking
RERANK_SIM_THRESHOLD = _settings.rerank_sim_threshold
LLM_TEMPERATURE = _settings.llm_temperature

QWEN_API_BASE = _settings.qwen_api_base
QWEN_API_KEY = _settings.qwen_api_key


def llm_endpoint(model: str, deepseek_key: str = ""):
    """按模型名路由 OpenAI 兼容端点，返回 (base_url, api_key)

    qwen 系模型走阿里云百炼兼容端点 + QWEN_API_KEY；
    其余（deepseek 系降级模型）走 DeepSeek 官方端点 + 传入的池化 key。
    所有 /chat/completions 直连点都必须经此函数取 base 和 key，
    否则主力/降级分属两家供应商时会拿错端点或密钥。
    """
    if model.startswith("qwen"):
        return QWEN_API_BASE, QWEN_API_KEY
    return DEEPSEEK_API_BASE, deepseek_key


def apply_llm_request_options(payload: dict, model: str) -> dict:
    """为供应商请求补模型级选项，避免调用方遗漏导致行为漂移。

    qwen 系默认关闭思考模式：民法典回答已经由检索和依据校验约束，额外
    reasoning_content 只增加首包延迟和 token 成本，不提升事实覆盖率。
    """
    if str(model).startswith("qwen"):
        payload.setdefault("enable_thinking", LLM_ENABLE_THINKING)
    return payload

SUMMARY_THRESHOLD = _settings.summary_threshold
HISTORY_LIMIT = _settings.history_limit
HISTORY_TTL = _settings.history_ttl
HISTORY_SUMMARY_TTL = _settings.history_summary_ttl
RAG_ANSWER_TOP_K = _settings.rag_answer_top_k

TOOL_TIMEOUT = _settings.tool_timeout
HTTP_TIMEOUT_SHORT = _settings.http_timeout_short
HTTP_TIMEOUT_MEDIUM = _settings.http_timeout_medium
HTTP_TIMEOUT_LONG = _settings.http_timeout_long
MAP_API_TIMEOUT = _settings.map_api_timeout
AMAP_API_KEY = _settings.amap_api_key
TURNSTILE_SITE_KEY = _settings.turnstile_site_key
TURNSTILE_SECRET = _settings.turnstile_secret
SAFETY_FILTER_MESSAGE = _settings.safe_filter_message
SAFETY_FILTER_MESSAGE_EN = _settings.safe_filter_message_en
# 答案级兜底/安全文案的「全语言集合」：语义缓存守门按成员判定而非相等，
# 否则英文请求吐出的兜底不会等于中文常量、被当正常答案钉进缓存（缓存污染）。
SAFETY_MESSAGE_VARIANTS = frozenset({SAFETY_FILTER_MESSAGE, SAFETY_FILTER_MESSAGE_EN})
FALLBACK_MESSAGE_VARIANTS = frozenset({DEEPSEEK_FALLBACK_MESSAGE, DEEPSEEK_FALLBACK_MESSAGE_EN})
# 空回答占位的中英两版集合：断线保存等逻辑要判「这是不是占位」，本地化后不能再等值比较中文常量。
EMPTY_ANSWER_VARIANTS = frozenset({EMPTY_ANSWER_MESSAGE, EMPTY_ANSWER_MESSAGE_EN})
SAFETY_FILTER_WORDS_FILE = _settings.safe_filter_words_file
DEFAULT_CITY = _settings.default_city
APP_ENV = (_settings.app_env or "").lower()
TRUST_PROXY_HEADERS = _settings.trust_proxy_headers


def _parse_trusted_proxy_cidrs(raw: str) -> tuple:
    """解析 TRUSTED_PROXY_CIDRS：逗号分隔，允许 host/bits 写法（strict=False）。

    非法网段直接启动失败而不是静默跳过：信任面配错若退化成"谁都不信"，登录锁、
    短信日限额、oauth 限频会塌成全站共用一个 IP 桶——正是本键要修的那个故障。
    """
    nets = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError as e:
            raise RuntimeError(
                f"TRUSTED_PROXY_CIDRS 含非法网段 {item!r}：{e}") from e
    return tuple(nets)


TRUSTED_PROXY_CIDRS: tuple = _parse_trusted_proxy_cidrs(
    _settings.trusted_proxy_cidrs)


def peer_is_trusted_proxy(host: str) -> bool:
    """socket 对端是否命中配置的精确反代网段（IP 桶与 HSTS 共用的唯一判据）。

    地址解析不了（Unix socket、空值）按不可信处理。双栈监听时 nginx 常以
    ::ffff:127.0.0.1 呈现，先还原 IPv4-mapped 再比对，否则本机回环反代会被
    误判成不可信、全站又退回一个桶（这个判据失效是静默的，必须堵住）。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return any(ip in net for net in TRUSTED_PROXY_CIDRS)


# IANA 保留的文档用网段（RFC 5737），仅作合成测试地址，公网不可路由
_DOCUMENTATION_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


def is_plausible_client_ip(value: str) -> bool:
    """转发头里的"客户端 IP"是否像真实公网访客（供 auth._client_ip 采用前过一遍）。

    反代命中 TRUSTED_PROXY_CIDRS 只说明"这条连接来自我们信的入口"，不说明入口
    写进来的头值合理：nginx 走 bridge 网段时访客自报的 CF-Connecting-IP 会被原样
    透传，内网/组播/::ffff: 这类脏值会把按 IP 的登录锁与短信日额度打进无意义桶。
    本判据只挡这类值，**不是安全边界**——伪造一个公网可路由地址仍然可能，真正
    封住伪造要靠"非 CF 来源连不到网关端口"（compose 不发布端口 + 安全组只放 CF）。
    """
    raw = (value or "").strip()
    if not raw:
        return False
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        return False
    # IANA 文档段（TEST-NET）在 Python 里也算 is_private，但伪造它只会落进一个
    # 不可路由的空桶，无害；本仓十余处测试夹具正是拿这些地址当合成访客 IP。
    if any(ip in net for net in _DOCUMENTATION_NETS):
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


METRICS_TOKEN_CFG = _settings.metrics_token
DB_ACQUIRE_TIMEOUT = _settings.db_acquire_timeout
DB_COMMAND_TIMEOUT = _settings.db_command_timeout
TASK_TIMEOUT = _settings.task_timeout
LLM_CONNECT_TIMEOUT = _settings.llm_connect_timeout
TASK_TTL_SECONDS = _settings.task_ttl_seconds

EMBEDDING_API_URL = _settings.embedding_api_url
EMBEDDING_TIMEOUT = _settings.embedding_timeout
EMBEDDING_API_KEY = _settings.embedding_api_key
EMBEDDING_MODEL = _settings.embedding_model
EMBEDDING_PROVIDER = _settings.embedding_provider.strip().lower()
EMBEDDING_DIMENSIONS = _settings.embedding_dimensions
CACHE_SIMILARITY_THRESHOLD = _settings.cache_similarity_threshold
CACHE_L0_TTL = _settings.cache_l0_ttl


def embedding_endpoint_candidates() -> list:
    """收集可用的 embeddings 端点候选（去空、去重、保序）。

    2026-09-12 修复（P0）：原实现把配置值直接当唯一候选发出去，配置为空串时
    请求 '' 报 "URL missing protocol" 后静默返回 None → `_recall_pg_vector`
    再静默返回 [] → **向量召回整条失效**。实测 55/55 条真实 trace 的 vector 腿
    hits 全为 0，长期无人发现（检索层当时零指标）。

    候选优先级：显式 EMBEDDING_API_URL → 由 OLLAMA_URL 推导（Ollama 早已配好且
    可达，原实现却完全不读它）。注意 Ollama 的嵌入接口是 /api/embeddings，
    与其生成接口 /api/generate 不是同一个端点，必须换而非直接复用。
    """
    if EMBEDDING_PROVIDER != "ollama":
        return [EMBEDDING_API_URL] if EMBEDDING_API_URL else []
    cands = []
    url = (EMBEDDING_API_URL or "").strip()
    if url:
        cands.append(url)
        # 两个主机名互为兜底：容器内连 localhost 是即时拒绝，宿主机上连
        # host.docker.internal 会黑洞等超时——故显式配置值优先，另一个在后
        for a, b in (("localhost", "host.docker.internal"),
                     ("host.docker.internal", "localhost")):
            if a in url:
                cands.append(url.replace(a, b))
                break
    ollama = (os.getenv("OLLAMA_URL") or "").strip()
    if ollama:
        base = ollama.split("/api/")[0].rstrip("/")
        if base:
            cands.append(f"{base}/api/embeddings")
            if "host.docker.internal" in base:
                cands.append(f"{base.replace('host.docker.internal', 'localhost')}/api/embeddings")
    seen, out = set(), []
    for u in cands:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

DAILY_REQUEST_LIMIT = _settings.daily_request_limit
DAILY_TOKEN_LIMIT = _settings.daily_token_limit
SECOND_REQUEST_LIMIT = _settings.second_request_limit
ADMIN_QPS_LIMIT = _settings.admin_qps_limit
ADMIN_DAILY_REQ = _settings.admin_daily_req
ADMIN_DAILY_TOKEN = _settings.admin_daily_token
ADMIN_CONCURRENT = _settings.admin_concurrent
USER_QPS_LIMIT = _settings.user_qps_limit
USER_DAILY_REQ = _settings.user_daily_req
USER_DAILY_TOKEN = _settings.user_daily_token
USER_CONCURRENT = _settings.user_concurrent

# 能力开关（2026-09-30 建了不闭环治理）：默认关，行为与历史等价
INTENT_USE_LLM = _settings.intent_use_llm
ORCH_ENABLE_PHASE2 = _settings.orch_enable_phase2

GITHUB_QUESTION_LIMIT = _settings.github_question_limit

ALIBABA_CLOUD_ACCESS_KEY_ID = _settings.alibaba_cloud_access_key_id.strip()
ALIBABA_CLOUD_ACCESS_KEY_SECRET = _settings.alibaba_cloud_access_key_secret.strip()
SMS_SIGN = _settings.sms_sign
SMS_TEMPLATE_CODE = _settings.sms_template_code
SMS_CODE_EXPIRE_SECONDS = _settings.sms_code_expire_seconds
SMS_SEND_MIN_INTERVAL = _settings.sms_send_min_interval
SMS_SEND_HOUR_LIMIT = _settings.sms_send_hour_limit
SMS_ATTEMPT_LIMIT = _settings.sms_attempt_limit
SMS_ATTEMPT_LOCK_SECONDS = _settings.sms_attempt_lock_seconds

ADMIN_PHONE = _settings.admin_phone
ADMIN_USERNAME = _settings.admin_username
ADMIN_PASSWORD = _settings.admin_password

GITHUB_REDIRECT_URI = _settings.github_redirect_uri
GATEWAY_PUBLIC_URL = _settings.gateway_public_url

BCRYPT_MAX_BYTES = _settings.bcrypt_max_bytes  # bcrypt 技术上限（存量截断兼容用，C11 去魔数）
PASSWORD_MAX_BYTES = _settings.password_max_bytes  # C7：新方案密码策略上限（SHA-256 预处理）

PAYMENT_SUCCESS_RATE = _settings.payment_success_rate
PAYMENT_SIMULATED_DELAY_MIN = _settings.payment_simulated_delay_min
PAYMENT_SIMULATED_DELAY_MAX = _settings.payment_simulated_delay_max
RECHARGE_MIN_AMOUNT = _settings.recharge_min_amount
RECHARGE_MAX_AMOUNT = _settings.recharge_max_amount
DEFAULT_WALLET_BALANCE = _settings.default_wallet_balance
ORDER_EXPIRE_SECONDS = _settings.order_expire_seconds
TOKEN_COST_RATE = _settings.token_cost_rate
