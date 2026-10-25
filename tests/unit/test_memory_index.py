from __future__ import annotations

from pathlib import Path

from kyle_agent.core.memory.index import MemoryIndex
from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore


# 功能:remember/recall 英文全文检索往返
# 设计:写入多条记忆后按关键词检索,断言命中正确条目与 source 元数据
def test_fts_recall_hit(tmp_path: Path) -> None:
    idx = MemoryIndex(tmp_path / "mem.db")
    idx.remember("deploy with docker compose", source="note", session_id="s1")
    idx.remember("prefer ruff over black", source="note", session_id="s1")
    hits = idx.recall("docker deploy")
    assert any("docker" in h.content for h in hits)
    assert hits[0].source == "note"
    idx.close()


# 功能:CJK 查询走 LIKE 兜底路径(unicode61 分词粒度不足)
# 设计:中文记忆 + 中文查询,FTS 可能 miss,断言兜底仍能命中子串
def test_cjk_recall_via_like_fallback(tmp_path: Path) -> None:
    idx = MemoryIndex(tmp_path / "mem.db")
    idx.remember("部署一律使用 docker", source="run", session_id="s1")
    hits = idx.recall("部署")
    assert any("docker" in h.content for h in hits)
    idx.close()


# 功能:空查询/空内容边界
# 设计:recall("") 返回空;remember("") 不产生条目
def test_empty_query_and_content(tmp_path: Path) -> None:
    idx = MemoryIndex(tmp_path / "mem.db")
    assert idx.recall("") == []
    idx.remember("   ", source="note")
    assert idx.recall("anything") == []
    idx.close()


# 功能:runner 全链路——记忆注入 system prompt + 成功结论回写
# 设计:预置一条与目标相关的记忆,CapturingProvider 断言 system 含
#       Recalled Memories 段;run 成功后断言记忆库新增 run 来源条目
async def test_runner_memory_inject_and_writeback(tmp_path: Path) -> None:
    from kyle_agent.core.config import KyleConfig
    from kyle_agent.core.llm.types import LlmResponse
    from kyle_agent.core.runner import AgentRunner

    class _EndTurn:
        async def chat(self, messages, tool_schemas, bus, run_id, *,
                       step=0, system=None, model=None, strategy="static"):
            self.system = system
            return LlmResponse(stop_reason="end_turn", text="部署使用 docker 完成")

    idx = MemoryIndex(tmp_path / "mem.db")
    idx.remember("the deploy password is DEPLOY-42", source="note", session_id="old")

    store = SessionStore(tmp_path / "sessions")
    session = Session(id="sess-mem", mode="chat", status="active", title="",
                      created_at="t", updated_at="t")
    store.write_meta(session)

    cfg = KyleConfig()
    provider = _EndTurn()
    runner = AgentRunner(cfg, provider=provider, runs_dir=tmp_path / "runs",
                         memory_index=idx)  # type: ignore[arg-type]
    await runner.run_and_capture("deploy the service", session=session, store=store)

    assert provider.system is not None
    assert "Recalled Memories" in provider.system
    assert "DEPLOY-42" in provider.system
    hits = idx.recall("docker")
    assert any(h.source == "run" and "docker" in h.content for h in hits)
    idx.close()
