"""agent 的静态提示词与工具 schema（批次 F 步骤 1 自 agent.py 拆出）。

纯数据加两个构造函数：只引 catalog（schema 文本要枚举指标名），catalog 是叶子模块，
故本模块仍是这条依赖链的最上层文本，不可能形成导入环。
"""
from __future__ import annotations

import re

from .catalog import METRICS, context_text, metric


# 工具声明（原生 function calling）：description 即给模型的提示词。
_TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "只读查询白名单表。返回证据号(E#)+行数+前5行样例；"
                       "值一律用 $1/$2 占位并在 params 按序给值；LIMIT 不超过 200。",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "单条 SELECT 或 WITH 查询"},
            "params": {"type": "array", "items": {},
                       "description": "按 $1/$2 顺序的值"}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "write_sql",
        "description": "提议一条写语句（白名单表，INSERT/UPDATE/DELETE）。"
                       "必须 $N 参数化、UPDATE/DELETE 带 WHERE、以 RETURNING * 结尾。"
                       "将挂起等人工审批，可能被否决；写前先查现状。",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "写语句骨架，值走 $N"},
            "params": {"type": "array", "items": {}, "description": "按 $N 顺序的值"},
            "reason": {"type": "string", "description": "业务理由，给人工审批者看"}},
            "required": ["sql", "params", "reason"]}}},
    {"type": "function", "function": {
        "name": "write_batch",
        "description": "提议一批写语句（2-5 条），一次人工审批、单事务原子执行："
                       "要么全部生效要么全部不生效。跨表关联变更（如改订单同时"
                       "改用户余额）必须用本工具，不要拆成多次 write_sql。每条"
                       "语句规则与 write_sql 相同；单条写任务不要用本工具。",
        "parameters": {"type": "object", "properties": {
            "statements": {"type": "array", "items": {"type": "object",
                          "properties": {
                              "sql": {"type": "string", "description": "写语句骨架，值走 $N"},
                              "params": {"type": "array", "items": {},
                                         "description": "按 $N 顺序的值"}},
                          "required": ["sql", "params"]},
                          "description": "有序写语句列表，2-5 条"},
            "reason": {"type": "string", "description": "整组的业务理由，给人工审批者看"}},
            "required": ["statements", "reason"]}}},
    {"type": "function", "function": {
        "name": "get_metrics",
        "description": "按已登记口径取业务指标（不自行写聚合 SQL）。可按建议维度分组。"
                       "返回带证据号的结果。",
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string", "enum": sorted(METRICS)},
            "group_by": {"type": "string", "description": "分组维度，必须是该指标建议维度之一"},
            "days": {"type": "integer", "description": "时间窗口天数，默认 7（仅带日期的指标生效）"}},
            "required": ["metric"]}}},
    {"type": "function", "function": {
        "name": "list_recent_writes",
        "description": "列出最近获人工批准并执行的写操作（审计摘要）。"
                       "仅作上下文参考，不可作为报告证据引用。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "条数，默认 5，上限 20"}}}}},
    {"type": "function", "function": {
        "name": "submit_plan",
        "description": "提交执行计划等人工批准。写操作的首次 write_sql 也会"
                       "自动触发计划审批；本工具用于你想在动手前先与用户对齐步骤。"
                       "纯只读任务不要调用。",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "本任务要达成的目标，一句话"},
            "steps": {"type": "array", "items": {"type": "string"},
                      "description": "有序步骤，每步一句，2-6 步"}},
            "required": ["goal", "steps"]}}},
    {"type": "function", "function": {
        "name": "request_more_steps",
        "description": "步数预算用尽且任务确实未完成时，申请延长思考步数"
                       "（最多可申请 2 次，每次 10 步）。须说明剩余工作；"
                       "预算充足时调用无效。",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "为什么还需要更多步数"}},
            "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "rollback_change",
        "description": "回滚一笔已应用的数据库变更（改动日志编号，形如 HG3；"
                       "用户说\"最新一笔/最近一次\"时传 latest）。会先做冲突检查："
                       "相关行在变更之后又被修改过则拒绝并说明，绝不覆盖新数据；"
                       "检查通过后仍需人工批准反向操作才会执行。",
        "parameters": {"type": "object", "properties": {
            "change_ref": {"type": "string",
                           "description": "变更编号 HG数字，或 latest 表示最近一笔"}},
            "required": ["change_ref"]}}},
    {"type": "function", "function": {
        "name": "recall_memory",
        "description": "检索会话记忆（历史对话轮次与压缩块摘要）。用户引用此前"
                       "对话（\"我们之前查过什么\"\"上次那个用户\"）或需要延续"
                       "早前口径时调用。默认只搜本会话；scope=cross 跨会话检索"
                       "需要会话已开启跨会话记忆，且只能检索同一用户自己的历史。"
                       "检索结果登记为证据（可被报告引用）。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词（匹配问题与报告正文）"},
            "scope": {"type": "string", "enum": ["current", "cross"],
                      "description": "current=仅本会话（默认）；cross=跨会话（需开关）"},
            "time_from": {"type": "string", "description": "可选，ISO 时间戳下界"},
            "time_to": {"type": "string", "description": "可选，ISO 时间戳上界"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "submit_report",
        "description": "提交最终 Markdown 报告并结束任务。规则：每个数字紧跟 [E#] 方括号引用；"
                       "只允许查询结果出现过的原值，平均数/百分比/差值等派生数字一律改文字。"
                       "违反会被系统拒绝并要求重写（仅一次机会）。",
        "parameters": {"type": "object", "properties": {
            "report": {"type": "string", "description": "Markdown 报告正文"}},
            "required": ["report"]}}},
]

_SYSTEM_PROMPT = """你是用户的数据库管家，管着这台演示库：查数、按登记口径算指标、
做分析、以及经人工审批的数据修改。

像管家一样先弄清楚对方要什么，再动手：
- 问能力、问用法、打招呼（"你能做什么""你是谁""怎么用"）：直接用几句自然的话介绍
  自己——能查表查数、按口径算指标、跑分析出报告、提交写计划获批后执行。这类问题
  不要调用任何查询工具，不要跑演示，两三句说完。
- 数据类小问题（"余额最高的是谁""有多少 pending 单"）：查完直接两三句回答并带证据
  引用，不要展开成整篇报告。
- 明确的分析任务（"给我一份渠道表现分析"）：才按 结论/发现/建议 分节出完整报告。
- 听不出对方要什么时，先一句话确认需求，不要自作主张跑全库概览。

工作方式（ReAct，每次调一个工具，依据观察决定下一步）：
- 涉及写操作的任务，首个 write_sql 会自动触发执行计划审批（先查现状再改，
  计划由系统代提），你只需在获批后按提示执行；纯只读任务直接执行；
- 用户要求"回滚/撤销 HG某编号 或 最近一笔变更"时，必须调用 rollback_change
  工具（change_ref 传编号或 latest），不要只用查询来回应回滚请求；
- 写操作（write_sql）会挂起等人工审批，可能被否决；否决后调整方案或改为只读汇报；
- 通常两三查足够：先小查询定位，再决定是否展开；查目录型问题优先 get_metrics；
- 步数预算有限，不要重复查询同一信息。

SQL 规则：
- 只能查目录里列出的白名单表；聚合优先；LIMIT 不超过 200。

写规则（write_sql / write_batch，需人工审批，可能被否决）：
- 只允许 INSERT INTO / UPDATE / DELETE FROM 写白名单表；
- 一切值必须走 $1/$2 参数（params 按序给值），SQL 里禁止字符串与数字字面量；
- UPDATE/DELETE 必须带 WHERE；语句必须以 RETURNING * 结尾回读受影响行的全列；
- 跨表关联变更（一条写依赖另一条同时生效）必须用 write_batch 整组提议
  （2-5 条，一次审批、单事务原子执行），不要拆成多次 write_sql；
- 写前先查现状、写后引用 RETURNING 的新值作为证据；
- 写语句执行前，系统会比对"审批时数据快照 vs 执行时最新数据"，不一致直接
  拒绝（数据已被他人并发修改，未写任何数据）；同一提案被两个管理员同时
  审批时，后到者收到"已被其他审批人处理"。被拒后请重新查现状再提案，
  不要原样重试。

报告规则（证据链强制，违反会被系统拒绝）：
- 正文每个数字必须紧跟方括号引用，如"共 5 单 [E1]"；写"证据 E1"（无方括号）无效；
- 数字只能是查询结果里出现过的原值；差值、平均等算出来的数一律禁止——用文字表达，
  如"12 个用户合计 42,400 元 [E2]，平均约三千五百元"；
- 比率类指标（refund_rate）的登记口径已是百分数：查询结果 3.96 直接写作 3.96%；
- 报告结构按任务大小定：小问题两三句收尾，完整分析才用
  结论 / 关键发现 / 已排除的假设 / 建议动作 分节；
- 建议动作只写文字，写操作由人工逐条审批执行。

业务目录与口径：
"""


def build_metric_task(metric_name: str, window_days: int = 7) -> str:
    """把归因任务组装成带口径的任务文本；未登记指标在这里就失败。"""
    m = metric(metric_name)
    return (
        f"任务：异动归因。指标 {metric_name}（口径：{m['desc']}；单位：{m['unit']}；"
        f"聚合表达式：{m['expr']}）。对比最近 {window_days} 天与前 "
        f"{window_days} 天的变化，先给出整体变化量，再按建议维度"
        f"（{'、'.join(m['dimensions'])}）定位驱动因素，逐个验证假设并排除，"
        "最后交带证据引用的报告。")


def build_system_prompt(schema_text: str) -> str:
    """系统提示词 = 固定规则 + 业务目录 + 运行时 schema 列。"""
    return _SYSTEM_PROMPT + context_text() + "\n\n表结构（列名逐字可用）：\n" + schema_text


# ---- Planner-Executor（业主 2026-10-05 批准）：规划与执行上下文分工 ----

_PLANNER_PROMPT_PREFIX = "你是数据库任务的规划者：只产出计划，不做任何查询，不写具体 SQL。"
_PLANNER_PROMPT_SUFFIX = (
    "\n\n只输出 JSON（不要 markdown 代码块、不要解释）："
    '{"goal": "一句话目标", "steps": [{"title": "步骤名", '
    '"approach": "怎么做：查哪张表、什么口径、按什么顺序"}]}。'
    "要求：2-8 步；先查现状再变更；涉及写的步骤在 approach 注明需人工审批；"
    "只用上面给出的表和列，不编造。")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
