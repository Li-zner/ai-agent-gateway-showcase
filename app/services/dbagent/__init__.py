"""中控台 DB Agent 包：只读/受审写双通道的数据库分析 agent。

来源：DBagent 独立项目（2026-10-02 搬入适配）。与独立版的差异：
LLM 走网关 chat_support（复用超时与 flash 回退）；连接池读网关 config
（DBAGENT_READONLY_DSN / DBAGENT_WRITE_DSN）；写确认从 CLI 交互改为
tasks.py 的挂起审批门（中控台异步批准）。边界不变：写三道门
（写守卫 -> 人工审批 -> writer 角色库级权限）。
"""
