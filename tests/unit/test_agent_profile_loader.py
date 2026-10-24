from __future__ import annotations

from pathlib import Path

import pytest

from kyle_agent.core.agents.loader import AgentProfileLoader


# 功能：内建 planner 角色配置应能被 AgentProfileLoader 加载
# 设计：直接调用 load("planner")，验证关键字段非空
def test_builtin_planner_found() -> None:
    loader = AgentProfileLoader()
    profile = loader.load("planner")
    assert profile is not None
    assert profile.name == "planner"
    assert profile.system_prompt != ""
    assert profile.allowed_tools is not None
    assert "read_file" in profile.allowed_tools or len(profile.allowed_tools) > 0


# 功能：内建三种角色均可加载
# 设计：参数化测试所有内建角色名
@pytest.mark.parametrize("role", ["planner", "executor", "reviewer"])
def test_all_builtin_roles_found(role: str) -> None:
    loader = AgentProfileLoader()
    profile = loader.load(role)
    assert profile is not None, f"builtin role '{role}' not found"
    # 每个内建角色都显式配置了非空 allowed_tools
    assert profile.allowed_tools is not None and len(profile.allowed_tools) > 0


# 功能：未知角色名应返回 None
# 设计：查找不存在的角色，断言返回 None 而非抛异常
def test_unknown_role_returns_none() -> None:
    loader = AgentProfileLoader()
    result = loader.load("nonexistent_role_xyz")
    assert result is None


# 功能：TOML 角色配置文件应被正确解析
# 设计：写入临时 TOML 文件，通过 _parse 解析并验证所有字段
def test_toml_parsed(tmp_path: Path) -> None:
    content = """\
[agent]
description = "测试角色"
system_prompt = "你是测试助手。"
allowed_tools = ["read_file", "bash"]
model = "claude-sonnet-4-6"
"""
    p = tmp_path / "tester.toml"
    p.write_text(content, encoding="utf-8")
    loader = AgentProfileLoader()
    profile = loader._parse(p, "tester")
    assert profile.name == "tester"
    assert profile.description == "测试角色"
    assert profile.system_prompt == "你是测试助手。"
    assert profile.allowed_tools is not None
    assert "read_file" in profile.allowed_tools
    assert "bash" in profile.allowed_tools
    assert profile.model == "claude-sonnet-4-6"


# 功能：项目本地角色配置应覆盖内建同名配置
# 设计：在 .kyle/agents/ 中写入同名 TOML，monkeypatch cwd，断言加载到本地版本
def test_project_overrides_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_agents = tmp_path / ".kyle" / "agents"
    local_agents.mkdir(parents=True)
    (local_agents / "planner.toml").write_text(
        '[agent]\ndescription = "local planner"\nsystem_prompt = "local prompt"\n'
        'allowed_tools = ["list_dir"]\nmodel = ""\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = AgentProfileLoader()
    profile = loader.load("planner")
    assert profile is not None
    assert profile.description == "local planner"
    assert profile.allowed_tools is not None
    assert "list_dir" in profile.allowed_tools


# 功能：allowed_tools 写成字符串时应被解析期拒绝，而不是被 set() 拆成字符集导致子 agent 静默失去全部工具
# 设计：_parse 直接断言 ValueError；再经 load() 的搜索路径验证解析失败返回 None（而非半解析的配置）
def test_allowed_tools_string_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "badrole.toml"
    p.write_text(
        '[agent]\ndescription = "bad"\nsystem_prompt = "x"\nallowed_tools = "bash"\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader()
    with pytest.raises(ValueError):
        loader._parse(p, "badrole")

    local_agents = tmp_path / ".kyle" / "agents"
    local_agents.mkdir(parents=True, exist_ok=True)
    (local_agents / "badrole.toml").write_text(
        '[agent]\ndescription = "bad"\nsystem_prompt = "x"\nallowed_tools = "bash"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert loader.load("badrole") is None


# 功能：未声明 allowed_tools 的角色配置解析为 None（不限制工具）
# 设计：写入不含 allowed_tools 键的 TOML，断言字段为 None；None 与空列表语义不同
def test_allowed_tools_absent_is_none(tmp_path: Path) -> None:
    p = tmp_path / "openrole.toml"
    p.write_text(
        '[agent]\ndescription = "open"\nsystem_prompt = "x"\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader()
    profile = loader._parse(p, "openrole")
    assert profile.allowed_tools is None
