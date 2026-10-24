from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    # None 表示不限制工具；空列表表示不允许任何工具；非空列表为白名单
    allowed_tools: list[str] | None = None
    model: str = ""


# 按两级优先级（项目本地 > 用户全局 > 内建）查找并解析角色配置
class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 查找指定角色配置；未找到返回 None
    def load(self, name: str) -> AgentProfile | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return self._parse(path, name)
                except Exception:
                    # 解析失败与"未找到"必须区分开，否则配置错误被静默吞掉
                    logger.warning("agent profile %s failed to parse", path, exc_info=True)
                    return None
        return None

    # 返回 [项目本地, 用户全局, 内建] 路径；load() 返回第一个存在的，项目本地优先级最高
    def _search_paths(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.toml"
        global_ = Path("~/.kyle/agents").expanduser() / f"{name}.toml"
        local = Path(".kyle/agents") / f"{name}.toml"
        return [local, global_, builtin]

    # 解析 TOML 角色配置文件；allowed_tools 类型错误时抛 ValueError
    def _parse(self, path: Path, name: str) -> AgentProfile:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        agent = data.get("agent", {})
        allowed_tools = agent.get("allowed_tools")
        if allowed_tools is not None and (
            not isinstance(allowed_tools, list)
            or not all(isinstance(item, str) for item in allowed_tools)
        ):
            # 字符串会被 set() 拆成字符集导致子 agent 静默失去全部工具，必须在解析期拒绝
            raise ValueError(
                f"agent '{name}': allowed_tools must be a list of tool names"
            )
        return AgentProfile(
            name=name,
            description=agent.get("description", ""),
            system_prompt=agent.get("system_prompt", "").strip(),
            allowed_tools=allowed_tools,
            model=agent.get("model", ""),
        )
