"""
人格/角色管理模块
- 从 prompts/ 目录加载不同人格的 System Prompt
- 支持运行时切换人格
- 人格包含：名称、图标、模型配置、System Prompt、工具列表
"""
import os
import json
from pathlib import Path
from typing import Optional
from ..core.logging import setup_logging
from ..core.config import LLM_PRIMARY_MODEL
from ..core.constants import get_persona_prompt, get_all_persona_ids

logger = setup_logging()

PERSONA_DIR = Path(__file__).parent.parent.parent / "prompts"


def is_civil_persona(persona_id) -> bool:
    """是否属于「民法典行为族」：civil_code 及其内部派生（如 civil_code_eval）。

    评测人格必须与线上民法典人格走完全相同的检索强制、路由裁决、零召回
    拒答等硬规则，否则评测结论不反映真实链路行为。所有原先硬编码
    `persona_id == "civil_code"` 的行为守卫统一改判此函数。
    注意：只用于行为判断，不用于展示名/知识库加载（那些走 get_effective）。
    """
    return (persona_id or "").startswith("civil_code")


# 评测人格 id（批量评测走真实 chat 端点时带的 persona）
EVAL_PERSONA_ID = "civil_code_eval"


def is_eval_persona(persona_id) -> bool:
    """是否**评测流量**人格。

    与 `is_civil_persona` 的分工别搞混：那一个是"民法典行为族"（含本评测人格，
    用于检索强制/路由裁决等硬规则，评测必须与线上同链路）；而统计与画像口径要的
    恰恰是**只排除评测、保留真实用户**——误用 `is_civil_persona` 会把线上民法典
    用户的画像一起关掉（批1 B-P1-3 登记的原由）。
    """
    return (persona_id or "").startswith(EVAL_PERSONA_ID)


class Persona:
    """一个人格（角色）"""

    def __init__(self, persona_id: str, name: str, icon: str,
                 system_prompt: str, model: str = "",
                 tools_enabled: list = None,
                 knowledge_base: str = "",
                 show_reasoning: bool = True):
        self.id = persona_id
        self.name = name
        self.icon = icon
        self.system_prompt = system_prompt
        self.model = model
        self.tools_enabled = tools_enabled or []
        self.knowledge_base = knowledge_base  # 关联的知识库文件路径
        self.show_reasoning = show_reasoning  # 是否展示思考过程（P2 #24 配置化）

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "model": self.model,
            "tools_enabled": self.tools_enabled,
            "knowledge_base": self.knowledge_base,
        }


class PersonaManager:
    """人格管理器 - 加载/切换人格"""

    def __init__(self):
        self._personas: dict[str, Persona] = {}
        self._current: Optional[str] = None
        self._load_personas()

    def _load_personas(self):
        """从 prompts/ 目录加载所有人格（使用缓存）"""
        persona_ids = get_all_persona_ids()
        for pid in persona_ids:
            try:
                data = get_persona_prompt(pid)
                if not data:
                    continue
                persona = Persona(
                    persona_id=data["id"],
                    name=data["name"],
                    icon=data.get("icon", ""),
                    system_prompt=data["prompt"],
                    model=data.get("model", ""),
                    tools_enabled=data.get("tools", []),
                    knowledge_base=data.get("knowledge_base", ""),
                    show_reasoning=data.get("show_reasoning", True),  # P2 #24：默认展示思考
                )
                self._personas[persona.id] = persona
                logger.info(f"加载人格: {persona.icon} {persona.name} (id={persona.id})")
            except Exception as e:
                logger.warning(f"加载人格 {pid} 失败: {e}")

        # 默认选中 travel（旅行规划助手）；人格已分化（travel/civil_code/me），无混合人格
        if self._personas and not self._current:
            self._current = "travel" if "travel" in self._personas else list(self._personas.keys())[0]
        # 降级兜底：prompts 目录缺失或解析全失败时，注册内置默认人格，避免 LLM 无 system prompt（P1 #67）
        if not self._personas:
            self._personas["unified"] = Persona(
                persona_id="unified",
                name="统一助手",
                icon="",
                system_prompt="你是AI助手，请直接、准确地回答用户的问题。今天是{today}。",
            )
            self._current = "unified"
            logger.warning("prompts 目录为空或解析失败，已注册内置默认人格（unified）")

    @property
    def current(self) -> Optional[Persona]:
        return self._personas.get(self._current) if self._current else None

    @property
    def current_id(self) -> Optional[str]:
        return self._current

    def switch(self, persona_id: str) -> bool:
        """切换人格（全局默认值；仅 /api/persona/switch 显式调用）"""
        if persona_id in self._personas:
            self._current = persona_id
            logger.info(f"切换到人格: {self.current.icon} {self.current.name}")
            return True
        logger.warning(f"人格不存在: {persona_id}")
        return False

    def get_effective(self, persona_id: Optional[str] = None) -> Optional[Persona]:
        """按请求解析人格：显式 persona_id 优先，未指定/不存在时回落全局默认。

        只读不改 _current——请求路径禁止调用 switch()（全局可变状态会被
        并发请求互相覆盖，导致 A 用户拿到 B 用户的人格 system prompt）。
        """
        if persona_id:
            p = self._personas.get(persona_id)
            if p:
                return p
        return self.current

    def list_personas(self) -> list[dict]:
        """列出所有人格（内部评测人格除外：id 以 _eval 结尾，仅供评测脚本
        显式指定，用于给监控流量打来源标记，不应出现在用户可选列表里）"""
        return [p.to_dict() for p in self._personas.values()
                if not p.id.endswith("_eval")]

    def get_persona(self, persona_id: str) -> Optional[Persona]:
        return self._personas.get(persona_id)

    def get_system_prompt(self, persona_id: Optional[str] = None) -> str:
        """获取指定人格的 System Prompt"""
        p = self._personas.get(persona_id) if persona_id else self.current
        if not p:
            return ""
        return p.system_prompt

    def get_model(self, persona_id: Optional[str] = None) -> str:
        p = self._personas.get(persona_id) if persona_id else self.current
        if p:
            return p.model or LLM_PRIMARY_MODEL
        return LLM_PRIMARY_MODEL

    def get_enabled_tools(self, persona_id: Optional[str] = None) -> list:
        p = self._personas.get(persona_id) if persona_id else self.current
        return p.tools_enabled if p else []

    def get_knowledge_base(self, persona_id: Optional[str] = None) -> str:
        p = self._personas.get(persona_id) if persona_id else self.current
        return p.knowledge_base if p else ""


# ============================================================
# 全局单例
# ============================================================
_manager = None


def _build_degraded_manager() -> PersonaManager:
    """降级单例：仅含内置默认人格（P1 #68：初始化失败时防全局状态异常）"""
    pm = PersonaManager.__new__(PersonaManager)  # 绕过 __init__（不加载 prompts）
    pm._personas = {}
    pm._current = "unified"
    pm._personas["unified"] = Persona(
        persona_id="unified", name="统一助手", icon="AI",
        system_prompt="你是AI助手，请直接、准确地回答用户的问题。今天是{today}。",
    )
    return pm


def get_persona_manager() -> PersonaManager:
    global _manager
    if _manager is None:
        try:
            _manager = PersonaManager()
        except Exception as e:
            # 初始化失败：记录错误并使用降级单例（P1 #68）
            logger.error(f"人格管理器初始化失败，使用降级单例: {e}")
            _manager = _build_degraded_manager()
    return _manager
