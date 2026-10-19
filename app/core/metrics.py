from prometheus_client import Counter, Histogram, Gauge

gateway_requests_total = Counter(
    'gateway_requests_total',
    'Total requests to gateway',
    ['method', 'endpoint', 'status']
)

gateway_request_duration_seconds = Histogram(
    'gateway_request_duration_seconds',
    'Request duration in seconds',
    ['method', 'endpoint']
)

gateway_errors_total = Counter(
    'gateway_errors_total',
    'Total errors by status code',
    ['status']
)

app_exceptions_total = Counter(
    'app_exceptions_total',
    'Unhandled exceptions aggregated by type',
    ['type']
)

# 慢查询观测（后台任务查 pg_stat_statements，见 core/slow_query_watch.py）
app_slow_queries_total = Gauge(
    'app_slow_queries_total',
    'Slow queries count from pg_stat_statements (mean_exec_time > threshold) in last check',
    ['threshold_ms']
)
app_slow_query_max_ms = Gauge(
    'app_slow_query_max_ms',
    'Max mean exec time (ms) of slow queries in last check'
)

llm_tokens_total = Counter(
    'llm_tokens_total',
    'LLM tokens consumed (total)',
    ['type']  # input / output
)

llm_tokens_detail = Counter(
    'llm_tokens_detail',
    'LLM tokens by model and endpoint',
    ['model', 'endpoint', 'type']  # model=LLM_PRIMARY_MODEL, endpoint=v2_chat/sub_agent, type=input/output
)

llm_requests_total = Counter(
    'llm_requests_total',
    'LLM API request count',
    ['model', 'endpoint', 'status']  # status=success/error
)

kb_search_total = Counter(
    'kb_search_total',
    'Knowledge base searches by source',
    ['source', 'result']  # source=civil_code/travel, result=hit/miss/decomposed
)

# 检索层专项指标（2026-09-12）：此前检索链路零指标，kb_search_total 定义后从未被调用，
# 导致"某条召回路长期失效"这类故障完全不可观测。
kb_recall_empty_total = Counter(
    'kb_recall_empty_total',
    '检索最终零返回的次数（召回彻底失败）',
    ['persona']
)

kb_low_confidence_total = Counter(
    'kb_low_confidence_total',
    '双路召回最高分均低于阈值（将触发一次 LLM 查询改写重试）'
)

kb_rerank_dropped_total = Counter(
    'kb_rerank_dropped_total',
    '有候选但未执行重排（开关关闭或模型不可用，静默退化为 RRF 序）'
)

kb_leg_empty_total = Counter(
    'kb_leg_empty_total',
    '单条召回路命中为空（其他路有命中时即为单腿降级）',
    ['leg']  # trgm / vector
)

kb_retrieval_latency_seconds = Histogram(
    'kb_retrieval_latency_seconds',
    '知识库检索端到端耗时（秒，含改写重试与重排）',
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
)

semantic_cache_hits_total = Counter(
    'semantic_cache_hits_total',
    'Semantic cache hits'
)

semantic_cache_misses_total = Counter(
    'semantic_cache_misses_total',
    'Semantic cache misses'
)

rate_limit_rejects_total = Counter(
    'rate_limit_rejects_total',
    'Rate limit rejects'
)

# fail-open 计数（2026-09-19 审查 core P2-6）：Redis 异常时四道闸按可用性优先放行，
# 故障窗口内 = 全量用户不受限的 LLM 成本敞口。此前只有 error 日志，无法触发人工限流
# 预案；gate ∈ {qps, concurrent, daily}，配套 alert.rules.yml RateLimitFailOpen。
rate_limit_failopen_total = Counter(
    'rate_limit_failopen_total',
    'Rate limit gates bypassed on Redis failure (fail-open)',
    ['gate']
)

# RAG 在线监测（2026-09-13 MVP）：ragclosure 规则引擎诊断结论计数。verdict 落
# rag_verdicts 表后同步打点，Prometheus/Alertmanager 侧即可对「high 级结论速率」
# 告警，无需 PG exporter。
rag_verdicts_total = Counter(
    'rag_verdicts_total',
    'RAG 监测诊断结论数（RC 规则代码 × 严重度）',
    ['code', 'severity']
)

# 请求级 trace 完整性（2026-09-14 Phase 1）：用于发现采集停摆、落库失败和
# 新旧链路关联缺失。项目标签只保留有界类别，禁止放 request_uid。
rag_request_trace_written_total = Counter(
    'rag_request_trace_written_total',
    'RAG 请求级 trace 成功落库数'
)

rag_request_trace_write_failed_total = Counter(
    'rag_request_trace_write_failed_total',
    'RAG 请求级 trace 落库失败数'
)

rag_request_trace_last_success_timestamp_seconds = Gauge(
    'rag_request_trace_last_success_timestamp_seconds',
    'RAG 请求级 trace 最近一次成功落库时间戳'
)

rag_request_trace_missing_total = Counter(
    'rag_request_trace_missing_total',
    'RAG trace 缺少 request_uid 的数量',
    ['kind']
)

rag_monitor_last_cycle_timestamp_seconds = Gauge(
    'rag_monitor_last_cycle_timestamp_seconds',
    'RAG 监测循环最近一次成功完成时间戳'
)

rag_action_worker_last_run_timestamp_seconds = Gauge(
    'rag_action_worker_last_run_timestamp_seconds',
    'RAG 修复动作 worker 最近一次轮询完成时间戳'
)

rag_action_executions_total = Counter(
    'rag_action_executions_total',
    'RAG 修复动作执行结果',
    ['risk_level', 'status']
)

# ============================================================
# 支付系统指标
# ============================================================
payment_orders_total = Counter(
    'payment_orders_total',
    '支付订单总数',
    ['order_type', 'status', 'payment_method']
)

payment_amount_total = Counter(
    'payment_amount_total',
    '支付金额总额（元）',
    ['order_type', 'payment_method']
)

payment_channel_duration_seconds = Histogram(
    'payment_channel_duration_seconds',
    '支付渠道处理耗时（秒）',
    ['channel_code']
)

payment_token_cost_total = Counter(
    'payment_token_cost_total',
    'Token 扣费统计',
    ['currency']
)

# ============================================================
# CDC（变更数据捕获）指标
# ============================================================
cdc_events_processed_total = Counter(
    'cdc_events_processed_total',
    'CDC 事件已落盘总数'
)

cdc_errors_total = Counter(
    'cdc_errors_total',
    'CDC worker 错误总数'
)

cdc_lag_seconds = Gauge(
    'cdc_lag_seconds',
    'CDC 处理延迟（当前时间 - 最新事件时间，秒）'
)
# DFA 过滤器超时放行次数（P2 修复：超时 fail-open 原先不可观测）
safety_filter_timeout_total = Counter(
    'safety_filter_timeout_total',
    'DFA safety filter scans aborted by timeout (content passed unfiltered)'
)

# CORE-2（2026-09-20 审查）：后台循环死亡此前只有 warning 日志，CDC/维护/补偿
# 循环静默退出后只能经 lag 间接发现。每循环退出计一次，配套 alert.rules.yml
# BackgroundLoopStopped（increase > 0 即告警）。reason ∈ {error, cancelled, returned}
background_loop_stopped_total = Counter(
    'background_loop_stopped_total',
    '后台循环退出次数（按循环名与原因）',
    ['loop', 'reason']
)

# ============================================================
# DB Agent 观测（2026-10-03 #6：任务计量/审批门/回滚/守卫/夜跑）
# ============================================================
dbagent_tasks_total = Counter(
    'dbagent_tasks_total',
    'DB Agent 任务终态数',
    ['status']  # done / failed
)
dbagent_task_duration_seconds = Histogram(
    'dbagent_task_duration_seconds',
    'DB Agent 任务端到端耗时（含人工审批等待）',
    buckets=(5, 15, 30, 60, 120, 240, 480, 960)
)
dbagent_task_steps = Histogram(
    'dbagent_task_steps',
    'DB Agent 任务步数（工具调用轮数）',
    buckets=(1, 2, 3, 4, 6, 8, 12, 16, 20, 30, 40)
)
dbagent_llm_tokens_total = Counter(
    'dbagent_llm_tokens_total',
    'DB Agent LLM token 消耗（含重试）',
    ['type']  # input / output
)
dbagent_llm_fallbacks_total = Counter(
    'dbagent_llm_fallbacks_total',
    'DB Agent 跨供应商兜底通道触发（P1-1）',
    ['outcome']  # invoked / success / failed
)
dbagent_gate_decisions_total = Counter(
    'dbagent_gate_decisions_total',
    'DB Agent 审批门决定',
    # decision=approved/rejected 是人工决定；auto_rejected 是 300s 超时自动否决（P1-3）
    ['gate', 'decision']
)
dbagent_rollbacks_total = Counter(
    'dbagent_rollbacks_total',
    '回滚执行结果',
    ['result']  # ok / failed / blocked（冲突拒绝）
)
dbagent_guard_rejects_total = Counter(
    'dbagent_guard_rejects_total',
    'SQL 守卫拒绝次数',
    ['kind']  # read / write
)
dbagent_nightly_eval_last_passed = Gauge(
    'dbagent_nightly_eval_last_passed',
    '最近一次黄金评测夜跑通过例数'
)
dbagent_nightly_eval_last_total = Gauge(
    'dbagent_nightly_eval_last_total',
    '最近一次黄金评测夜跑总例数'
)
dbagent_nightly_eval_last_run_timestamp_seconds = Gauge(
    'dbagent_nightly_eval_last_run_timestamp_seconds',
    '黄金评测夜跑最近完成时间戳'
)

# 告警出网（#7）：webhook 推送结果
alerting_sent_total = Counter(
    'alerting_sent_total',
    '告警 webhook 推送结果',
    ['result']  # sent / failed
)
