"""表白名单 + 指标字典：DB Agent 的业务口径唯一真值源（人审 diff 文件）。

来源：DBagent 项目 dbagent/catalog.py（2026-10-02 搬入）。
当前只登记演示三表（假库测试期）；接真实业务表时在 TABLES 与 WRITE_TABLES
各加一行并同步角色授权——真实表进写白名单必须人类显式批准。
"""
from __future__ import annotations

# 表白名单：key 是真实表名，value 是给模型看的用途说明。
TABLES: dict[str, str] = {
    "demo_daily_orders": (
        "演示用日粒度渠道指标表。主键 (date, channel)；gmv 单位为元（退款前口径）；"
        "date 是自然日；channel 取值 app/social/web/miniapp/offline（app 量级最大，"
        "miniapp/offline 为新渠道）。用于归因循环的开发与验收"),
    "demo_users": (
        "演示用户表。主键 user_id；username 唯一；balance 单位元；"
        "status 取值 active/frozen。敏感列 phone/fake_password 已在库级收回"
        "（P1-4 列级白名单，查询即权限拒绝）；需要引用手机号时查 "
        "demo_users_masked.phone_code（确定性代号）"),
    "demo_users_masked": (
        "demo_users 的脱敏出口视图（只读）。phone_code 是手机号的确定性代号"
        "（U+6 位十六进制，同一号码恒为同一代号，可跨任务对齐引用）；"
        "视图不含手机号与密码原值。回答\"某用户的手机号\"类问题时，"
        "给代号并说明原值受保护"),
    "demo_orders": (
        "演示订单表。主键 order_no（形如 ORD-1001）；user_id 关联 demo_users；"
        "amount 单位元；status 取值 paid/pending/refunded"),
    "biz_customers": (
        "业务主表：模拟会员表（影子期实战）。主键 customer_id（1..100000）；"
        "customer_no 形如 CUST-000001；balance 单位元（随编号递增）；"
        "status 取值 active/frozen（每第 7 个为 frozen）。"
        "**用户/会员/客户类泛指问题（\"哪个用户最有钱\"\"会员数\"）默认查本表**——"
        "它是业务真表（十万行级）；demo_users 只是 200 行演示靶场表。"
        "敏感列 phone 已在库级收回（列级白名单，查询即权限拒绝）；"
        "需要引用手机号时查 biz_customers_masked.phone_code（确定性代号）"),
    "biz_customers_masked": (
        "biz_customers 的脱敏出口视图（只读）。phone_code 是手机号的确定性"
        "代号（U+6 位十六进制，同一号码恒为同一代号）；视图不含手机号原值。"
        "回答\"某会员的手机号\"类问题时，给代号并说明原值受保护"),
}

# 写表白名单：审批通过后可写的表（与 dbagent_writer 角色授权一一对应）。
# biz_customers 2026-10-04 影子期实战扩表（业主批准口径见修复日志）。
WRITE_TABLES: frozenset = frozenset(
    {"demo_daily_orders", "demo_users", "demo_orders", "biz_customers"})

# 指标字典：指标名 -> 表达式/单位/口径说明/建议归因维度。
# table/date_col（2026-10-03 新增）：get_metrics 工具据此装配聚合语句——
# 表名/时间列是代码内登记的受信标识符，不来自模型输入。
METRICS: dict[str, dict] = {
    "gmv": {
        "table": "demo_daily_orders", "date_col": "date",
        "expr": "SUM(gmv)",
        "unit": "元",
        "desc": "退款前口径的成交额（demo_daily_orders.gmv 直接求和）",
        "dimensions": ["channel"],
    },
    "orders": {
        "table": "demo_daily_orders", "date_col": "date",
        "expr": "SUM(orders)",
        "unit": "单",
        "desc": "支付成功订单数（按日粒度行求和）",
        "dimensions": ["channel"],
    },
    "refund_rate": {
        "table": "demo_daily_orders", "date_col": "date",
        "expr": "ROUND(SUM(refunds)::numeric * 100 / NULLIF(SUM(orders), 0), 2)",
        "unit": "%",
        "desc": "退款率百分数：退款单数 / 订单数 ×100，四舍五入两位小数"
                "（同窗口求和后相除，不是逐行比率平均）；结果 3.96 写作 3.96%",
        "dimensions": ["channel"],
    },
    "demo_balance": {
        "table": "demo_users", "date_col": None,
        "expr": "SUM(balance)",
        "unit": "元",
        "desc": "演示用户余额合计（demo_users.balance 求和，含 frozen 用户）",
        "dimensions": ["status"],
    },
}


def metric(name: str) -> dict:
    """取指标定义；未登记的指标直接抛错而不是让模型自由发挥口径。"""
    try:
        return METRICS[name]
    except KeyError as exc:
        raise ValueError(
            f"指标 {name} 未登记（可用: {', '.join(sorted(METRICS))}）。"
            "新指标先在 catalog.py 登记 口径，再提问。") from exc


def context_text() -> str:
    """拼出注入提示词的目录文本：表用途 + 指标口径。schema 列名由运行时另取。"""
    table_lines = [f"- {t}：{usage}" for t, usage in TABLES.items()]
    metric_lines = [
        f"- {name}：{m['expr']}（{m['unit']}）。口径：{m['desc']}。"
        f"建议归因维度：{'、'.join(m['dimensions'])}"
        for name, m in METRICS.items()]
    return ("可用表（白名单之外的表不可查）：\n" + "\n".join(table_lines)
            + "\n\n已登记指标（口径以此为准）：\n" + "\n".join(metric_lines))
