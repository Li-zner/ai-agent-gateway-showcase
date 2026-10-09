"""DB Agent 步数预算状态机（自 agent.py 的 ReAct 主循环拆出）。

拆出动机：`run_agent_task` 原为 104 行，违反「函数 ≤80 行」门禁；预算逻辑
（软上限、宽限期、延长申请额度、临预算提醒）是内聚的一段，且 agent.py 已
532 行、再放内部类会顶破「文件 ≤600 行」。行为与拆分前的内联实现逐字等价，
回归锚见 tests/unit/test_dbagent_agent_loop.py。

语义（2026-10-03 业主定稿）：上限只是软约束——预算用尽时模型可调用
request_more_steps 申请延长（最多 2 次、每次 10 步）；宽限期给模型响应
"收尾还是延长"的机会，宽限也耗尽才判定失败。步数不对用户显示。
"""
from __future__ import annotations

MAX_STEP_EXTENSIONS = 2   # request_more_steps 最多批准次数
EXTENSION_STEPS = 10      # 每次批准延长的步数
GRACE_STEPS = 4           # 预算用尽后给模型"收尾或申请延长"的宽限步数
NEAR_BUDGET_REMINDER = 3  # 剩余步数 <= 此值时提醒收尾或申请延长


class StepBudget:
    """循环步数预算：判耗尽、发宽限引导、批准/拒绝延长、发临预算提醒。"""

    def __init__(self, max_steps: int) -> None:
        self.total = max_steps
        self.extensions_used = 0
        self.extension_prompted = False

    def exhausted(self, step: int) -> bool:
        """预算与宽限都耗尽——循环该判定失败退出。"""
        return step > self.total + GRACE_STEPS

    def grace_prompt(self, step: int) -> str | None:
        """首次越过预算时给一次"收尾还是申请延长"的引导（只发一次）。"""
        if step <= self.total or self.extension_prompted:
            return None
        self.extension_prompted = True
        return ("步数预算已用尽。如果任务确实未完成，调用 request_more_steps 并说明理由"
                "申请延长步数；如果已经可以收尾，立即 submit_report。")

    def grant(self, args: dict) -> tuple[str, str | None]:
        """批准或拒绝一次延长申请，返回（回喂给模型的观察, 进度行或 None）。"""
        if self.extensions_used >= MAX_STEP_EXTENSIONS:
            return (f"步数延长次数已用完，最多可申请 "
                    f"{MAX_STEP_EXTENSIONS} 次，请立即 submit_report 收尾。", None)
        reason = str(args.get("reason") or "")[:80]
        self.total += EXTENSION_STEPS
        self.extensions_used += 1
        return (f"已批准延长 {EXTENSION_STEPS} 步，请继续完成任务。",
                f"步数延长 +{EXTENSION_STEPS}｜理由：{reason or '未说明'}")

    def nearing(self, step: int, fname: str) -> str | None:
        """临预算提醒：收尾或申请延长，二选一（不强制、只提示）。

        fname == "submit_report" 时静默：收稿被证据链驳回属正常纠错，此时催
        收尾只会让模型分心（保留拆分前的例外）。
        """
        remaining = self.total - step
        if fname == "submit_report" or remaining > NEAR_BUDGET_REMINDER:
            return None
        return (f"\n提醒：步数预算只剩 {remaining} 步，"
                "请尽快 submit_report 收尾，或调用 "
                "request_more_steps 申请延长。")
