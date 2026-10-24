from __future__ import annotations

from pathlib import Path

from kyle_agent.core.session.model import Session
from kyle_agent.core.session.store import SessionStore


# 功能：验证 SessionStore 初始化时自动创建 sessions 根目录
# 设计：传入 tmp_path 下不存在的目录，断言目录被创建，覆盖首次启动 daemon 的冷路径
def test_store_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    SessionStore(root)
    assert root.exists()


# 功能：验证 session meta 写入后能完整读回
# 设计：构造含 run_ids 的 Session，经过 JSON 文件往返后断言字段保持，覆盖 meta.json 的持久化契约
def test_meta_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = Session(
        id="sess-1",
        mode="chat",
        status="waiting_for_input",
        title="hello",
        created_at="t1",
        updated_at="t2",
        run_ids=["run-1"],
    )
    store.write_meta(session)
    loaded = store.read_meta("sess-1")
    assert loaded == session


# 功能：验证含 tool_use/tool_result block 的 thread 消息能按 Anthropic 格式读回
# 设计：追加 assistant tool_use 和 user tool_result，读取时应剥离 ts/run_id，只保留 API messages 所需字段
def test_thread_message_roundtrip_with_tool_blocks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "read file")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}],
        run_id="run-1",
    )
    store.append_message(
        "sess-1",
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        run_id="run-1",
    )

    messages = store.read_messages("sess-1")
    assert messages == [
        {"role": "user", "content": "read file"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]


# 功能：验证 thread 尾部孤儿 tool_use 会被裁掉
# 设计：构造一条未配对 tool_result 的 assistant tool_use，读取时只返回最后一次配平之前的消息，避免 API 报 messages.invalid
def test_read_messages_trims_orphan_tool_use_tail(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "hello")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}}],
        run_id="run-1",
    )
    assert store.read_messages("sess-1") == [{"role": "user", "content": "hello"}]


# 功能：验证 notes.md 不存在时读为空，追加笔记后能读到内容和 run_id
# 设计：先读空状态再追加，覆盖 chat 第一轮前和 note_save 调用后的两个关键状态
def test_notes_read_and_append(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.read_notes("sess-1") == ""
    store.append_note("sess-1", "Python 3.12", "run-1")
    notes = store.read_notes("sess-1")
    assert "Python 3.12" in notes
    assert "run-1" in notes


# 功能：write_compacted 重写后 read_messages 恒为平衡消息，且备份文件存在
# 设计：先写入带孤儿 tool_use 的 thread（模拟取消 run 的残留），再整表压缩重写；
#       断言读回的消息无孤儿、备份 .bak 存在，覆盖 runner 持久化前的裁剪契约
def test_write_compacted_leaves_balanced_thread(tmp_path: Path) -> None:
    from kyle_agent.core.session.store import SessionStore

    store = SessionStore(tmp_path)
    store.append_message("s1", "user", "hi")
    store.append_message(
        "s1",
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}],
    )
    store.write_compacted(
        "s1",
        [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "ack"},
        ],
    )
    messages = store.read_messages("s1")
    assert messages == [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "ack"},
    ]
    assert list((tmp_path / "s1").glob("thread_*.jsonl.bak"))


# 功能：同一秒内连续两次 write_compacted 不因备份文件重名而崩溃（Windows FileExistsError）
# 设计：紧连两次压缩重写，备份名带微秒时间戳，断言两个备份文件共存且线程可读
def test_write_compacted_twice_same_second(tmp_path: Path) -> None:
    from kyle_agent.core.session.store import SessionStore

    store = SessionStore(tmp_path)
    store.append_message("s1", "user", "hi")
    for _ in range(2):
        store.write_compacted(
            "s1",
            [
                {"role": "user", "content": "summary"},
                {"role": "assistant", "content": "ack"},
            ],
        )
    assert len(list((tmp_path / "s1").glob("thread_*.jsonl.bak"))) == 2
    assert len(store.read_messages("s1")) == 2
