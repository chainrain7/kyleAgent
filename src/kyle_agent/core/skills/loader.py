from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    # None 表示不限制工具；空列表表示不允许任何工具；非空列表为白名单
    allowed_tools: list[str] | None = None


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 识别 frontmatter 中的 `key:` 行（用于结束 allowed_tools 块列表的收集）
_KEY_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*.*$")


# 解析 Markdown skill 文件，提取 frontmatter 和正文 system prompt
def _parse_skill_file(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    allowed_tools: list[str] | None = None
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        front = m.group(1)
        body = text[m.end():]
        lines = front.splitlines()
        i = 0
        # 是否正在收集 allowed_tools 的块列表项；遇到下一个 key: 行即停止，
        # 防止其他键（如 steps:）的列表项被误吞进工具白名单
        collecting_tools = False
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:"):].strip().strip('"').strip("'")
                collecting_tools = False
            elif stripped.startswith("description:"):
                val = stripped[len("description:"):].strip().strip('"').strip("'")
                # YAML 块标量：> (折叠) 或 | (保留换行)，后续缩进行是内容
                if val in (">", "|"):
                    fold = val == ">"
                    parts: list[str] = []
                    i += 1
                    while i < len(lines) and (
                        lines[i].startswith(" ") or lines[i].startswith("\t")
                    ):
                        parts.append(lines[i].strip())
                        i += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                else:
                    description = val
                collecting_tools = False
            elif stripped.startswith("allowed_tools:"):
                # 显式声明即启用白名单（空 = 不允许任何工具）
                allowed_tools = []
                collecting_tools = True
                # 支持内联写法 allowed_tools: [read_file, bash]；空 [] 保持全禁
                inline = stripped[len("allowed_tools:"):].strip()
                if inline.startswith("[") and inline.endswith("]"):
                    inner = inline[1:-1].strip()
                    if inner:
                        allowed_tools.extend(
                            item.strip().strip("'\"")
                            for item in inner.split(",")
                            if item.strip()
                        )
            elif _KEY_LINE_RE.match(stripped):
                # 其他键出现，结束 allowed_tools 块列表的收集
                collecting_tools = False
            elif stripped.startswith("- "):
                if collecting_tools and allowed_tools is not None:
                    allowed_tools.append(stripped[2:].strip())
            i += 1

    return Skill(
        name=name,
        description=description,
        system_prompt_template=body.strip(),
        allowed_tools=allowed_tools,
    )


# 按三级优先级（项目本地 > 用户全局 > 内建）查找并解析 skill
class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 按优先级查找 skill 文件；未找到返回 None
    def resolve(self, name: str) -> Skill | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return _parse_skill_file(path)
                except Exception:
                    # 解析失败与"未找到"必须区分开，否则 skill 内容错误被静默吞掉
                    logger.warning("skill %s failed to parse", path, exc_info=True)
                    return None
        return None

    # 返回候选路径列表，同时支持扁平文件（name.md）和目录式（name/SKILL.md）两种格式
    def _search_paths(self, name: str) -> list[Path]:
        dirs = [
            Path(".kyle/skills"),
            Path("~/.kyle/skills").expanduser(),
            self._BUILTIN_DIR,
        ]
        paths: list[Path] = []
        for d in dirs:
            paths.append(d / f"{name}.md")
            paths.append(d / name / "SKILL.md")
        return paths

    # 列出所有可用 skill 名称（内建 + 用户全局 + 项目本地，去重后以项目本地覆盖为准）
    def list_all(self) -> list[str]:
        seen: dict[str, None] = {}
        for d in [
            self._BUILTIN_DIR,
            Path("~/.kyle/skills").expanduser(),
            Path(".kyle/skills"),
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    seen[f.stem] = None
                for f in sorted(d.glob("*/SKILL.md")):
                    seen[f.parent.name] = None
        return list(seen)

    # 列出所有可用 Skill 对象（含描述），项目本地覆盖同名内建
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for d in [
            self._BUILTIN_DIR,
            Path("~/.kyle/skills").expanduser(),
            Path(".kyle/skills"),
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    try:
                        skill = _parse_skill_file(f)
                        seen[skill.name] = skill
                    except Exception:
                        logger.warning("skill %s failed to parse", f, exc_info=True)
                for f in sorted(d.glob("*/SKILL.md")):
                    try:
                        skill = _parse_skill_file(f)
                        seen[skill.name] = skill
                    except Exception:
                        logger.warning("skill %s failed to parse", f, exc_info=True)
        return list(seen.values())

    # 将 $ARGUMENTS 替换为用户传入的参数字符串
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
