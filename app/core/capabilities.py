"""能力注册表与判定：admin 之外按 users.permissions 允许清单授权。

2026-10-03 业主定稿：管理员可对子账号做功能约束（允许/不允许查询、
批准、回滚等）。语义：
- role=admin 恒拥有全部能力（不受 permissions 限制，不可自我降级）；
- 非 admin 走 users.permissions 允许清单（fail-closed：未列即无）；
- users.permissions 是既有的 text[] 列，此前无消费者，本模块赋予其语义。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..middleware.auth import get_current_user

# 能力注册表：key -> (给管理员看的标签, 说明)。新增能力在此加一行，
# 管理端 UI 与端点守卫都从这张表驱动。
CAPABILITIES: dict[str, tuple[str, str]] = {
    "dbagent.view": ("查看 DB Agent", "查看任务时间线与改动日志（只读）"),
    "dbagent.ask": ("提交任务", "向 DB Agent 提交查询/分析/写任务"),
    "dbagent.approve": ("审批操作", "批准或否决执行计划与写提案"),
    "dbagent.write_delete": ("删除数据", "允许 DB Agent 提议 DELETE 语句"
                             "（生成前拦截；admin 恒有，无需勾选）"),
    "dbagent.rollback": ("执行回滚", "回滚已应用的变更（HG 编号）"),
    "rag.ask": ("诊断问答", "使用诊断助手（含只读问数）"),
}

# 子账号默认能力：能看不能动（管理员按需勾选放开）
DEFAULT_SUB_CAPS: list[str] = ["dbagent.view"]


def has_cap(user: dict, cap: str) -> bool:
    """admin 恒有；否则查 permissions 允许清单（fail-closed）。"""
    if user.get("role") == "admin":
        return True
    return cap in (user.get("permissions") or [])


def require_cap(cap: str):
    """FastAPI 依赖工厂：当前用户无该能力 -> 403（人话说明缺哪个能力）。"""
    label = CAPABILITIES.get(cap, (cap, ""))[0]

    async def _guard(current_user: dict = Depends(get_current_user)) -> dict:
        if not has_cap(current_user, cap):
            raise HTTPException(
                status_code=403,
                detail=f"需要「{label}」能力（{cap}），请联系管理员在权限管理中开通")
        return current_user

    return _guard
