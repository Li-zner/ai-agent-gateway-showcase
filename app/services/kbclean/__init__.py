# -*- coding: utf-8 -*-
"""文档清洗闸口层（SOP 批次 A 起建）。

对应 `agent_docs/2026-10-12-文档清洗SOP闭环计划.md`：本包只放**闸口与判据**
（能不能放行、缺了哪几条、重复了哪些键），不放清洗动作本身——清洗与拆分留在
`app/agents/`，落库与回滚留在 `app/services/rag_change_sets.py`，避免同一件事两处实现。
"""
