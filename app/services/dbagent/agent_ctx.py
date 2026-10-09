"""一次任务的可变上下文与三个异常（批次 F 步骤 1 自 agent.py 拆出）。

放在这条链的最底层：prompts/tools/planner/agent 都可以引它，它不引任何兄弟，所以拆完之后不可能形成导入环。
"""
from __future__ import annotations

import inspect
import json

from .evidence import EvidenceLog
from .masking import RowMasker


class MaxStepsExceeded(RuntimeError):
    """步数预算用尽；携带已完成证据摘要供排查。"""


class TokenBudgetExceeded(RuntimeError):
    """单任务 token 预算用尽（P2-3）；_run_task 捕获后转 failed 留痕。"""


class WriteBlocked(RuntimeError):
    """写执行被复检阻止（生产评审⑤）：审批预览与执行时数据不一致——
    未写任何数据。tasks 写包装器抛出，agent 换成模型可读的观察文本。"""


async def _maybe_await(result):
    """兼容同步与异步回调（CLI 同步返回 bool；中控台门返回协程）。"""
    if inspect.isawaitable(result):
        result = await result
    return result


class _RunCtx:
    """单次任务循环的共享上下文（工具执行器与循环主体之间）。"""

    report_retries_left = 2  # 报告被证据链拒绝后的重写余量
    plan_approved = False    # 计划门状态（写前硬闸依赖，见 _tool_write_sql）
    propose_only = False     # 影子期：写门批准后只登记不执行（#10）
    token_budget = 0         # 单任务 token 预算（P2-3，0=不设限）

    def __init__(self, db_fetch, db_write, confirm_write, confirm_plan,
                 log: EvidenceLog, progress, propose_only: bool = False,
                 db_write_batch=None, confirm_write_batch=None,
                 forced_propose=None, should_stop=None, token_budget: int = 0,
                 approval_actor=None, memory=None, allow_delete: bool = True,
                 actor: str = "", is_admin: bool = False):
        self.db_fetch, self.db_write = db_fetch, db_write
        self.confirm_write, self.confirm_plan = confirm_write, confirm_plan
        # 批提案组通道（P1-2）：None = 批通道关闭（CLI 等场景降级提示）
        self.db_write_batch, self.confirm_write_batch = db_write_batch, confirm_write_batch
        self.log, self.progress = log, progress
        self.propose_only = propose_only
        # P1-5：L2 高危提案强制转人工——门批准后求值（那时才有风险等级），
        # 作用于写/批写与回滚的执行判定（台账 449 后半）；任务级 propose_only 另管影子期
        self.forced_propose = forced_propose
        # P2-2：() -> bool，每步循环头求值；True 即停（废弃任务僵尸终止）
        self.should_stop = should_stop
        self.token_budget = int(token_budget or 0)
        # 深审 A17：() -> str，最近一道门批准的真实审批人（审计溯源用）
        self.approval_actor = approval_actor
        # 会话记忆（2026-10-05）：{"session_id","actor","allow_cross"}；
        # None = 本任务未关联会话（recall_memory 降级提示）
        self.memory = memory
        # 生成前 RBAC 动词闸（生产评审⑥）：False = 提交人无 dbagent.write_delete
        # 能力，DELETE 提案在守卫即拒（不等人工审批兜底）
        self.allow_delete = bool(allow_delete)
        # 提交人身份与是否最高管理员（业主 2026-10-05 拍板）：回滚归属校验用
        self.actor = actor
        self.is_admin = bool(is_admin)
        # 任务级遮蔽器：敏感值进 LLM 上下文前代号化/掩码（同一任务内代号稳定）
        self.masker = RowMasker()
        # 任务级 token 计量（chat 每次调用累加，终态随 result 上报）
        self.usage: dict = {}
        # (工具名, 规范化参数) —— 重复调用检测；request_more_steps 不参与（见循环）
        self.last_call: tuple[str, str] | None = None

    def duplicate(self, fname: str, args: dict) -> bool:
        """与上一次调用逐字相同则判重复（不更新基线，与原内联实现一致）。"""
        key = (fname, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key == self.last_call:
            return True
        self.last_call = key
        return False
