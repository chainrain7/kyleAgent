# AGENT.md

This file provides guidance to codex when working with code in this repository.

## Commands

```bash
# Install / sync dependencies
uv sync

# Lint
uv run ruff check src tests scripts
uv run mypy src

# Tests
uv run pytest tests/unit -v           # unit only (fast, no daemon)
uv run pytest tests/integration -v    # needs no running daemon; fixture spawns one
uv run pytest tests/ -v               # all

# Single test
uv run pytest tests/unit/test_envelope.py::test_request_roundtrip -v

# Regenerate WIRE_PROTOCOL.md after changing bus models
uv run python scripts/gen_protocol_doc.py

# Verify WIRE_PROTOCOL.md is in sync (used in CI equivalent)
uv run python scripts/gen_protocol_doc.py --check

# Run daemon manually
uv run kyle-core                        # foreground; Ctrl+C to stop
KYLE_PORT=8000 uv run kyle-core        # override port

# Runbook retrieval quality eval (offline, local hashing embedder, no key)
uv run python scripts/eval_rag.py

# Send a ping
uv run kyle ping
uv run kyle --version
```

## Architecture

This is a **dual-process** local AI agent system. `kyle-core` is a persistent daemon; `kyle` and `kyle-tui` are clients that connect to it over TCP (127.0.0.1:7437).

```
kyle-core (daemon)
  └─ listens on 127.0.0.1:7437 (TCP)
       ↑ JSON-RPC 2.0 NDJSON
kyle (CLI)   kyle-tui (TUI, S2+)
```

**`kyle-tui` is the primary frontend.** All user-facing work on task management, observability, and interaction should be designed for and validated in the TUI first. The `kyle` CLI exists only for quick scripted testing and debugging — it is not a product surface. When implementing features that touch the user interface, invest in the TUI layout, event rendering, and keyboard interactions. Do not shortcut TUI work by pointing to the CLI as an alternative.

### Current capability snapshot (2026-10-25, keep in sync with README)

Beyond the staged S0–S7 base, the runtime now ships:

- **Evals** (`evals/` + `scripts/run_evals.py`): 10 deterministic tasks judged by
  workspace end-state assertions; cassette record/replay gives offline, zero-key
  deterministic regression (CI gate: `tests/integration/test_eval_replay.py`).
  Changing tool *semantics* requires re-recording cassettes; new tasks require a
  cassette or the coverage test fails.
- **Context engine**: cl100k_base token accounting (`core/compact/tokens.py`),
  message-level cache breakpoints + `cache_hit_rate` metric, cross-session FTS5
  memory (`core/memory/index.py`, injected as Recalled Memories), structured
  JSON compaction + incremental keep-recent-turns.
- **Execution engine**: parallel read-only tool batches (`agent.max_parallel_tools`),
  durable runs (`core/durable.py` — per-step `run_state.json` snapshots, resume on
  daemon restart, at-least-once tool semantics).
- **Model routing** (`core/llm/router.py`) and **OpenAI-compatible backend**
  (`core/llm/openai_provider.py` + `factory.py`).
- **tiktoken offline** (CI fix): `core/compact/tokens.py` sets
  `TIKTOKEN_CACHE_DIR` to an in-package cache BEFORE importing tiktoken; the
  1.6MB cl100k_base blob is committed under `core/compact/tiktoken_cache/`.
  First `get_encoding` in a fresh CI runner must not hit the network (it either
  fails fast on linux/mac or hangs forever on windows runners otherwise).
  User-set TIKTOKEN_CACHE_DIR wins (setdefault).
- **OnCallCopilot** (complete, Phases 0–5): quantified report at docs/report/metrics.md
  and resume/interview deliverables at docs/career/resume.md — keep their numbers in sync
  with evals results when behavior changes.
- **Vector RAG engine** (`core/rag/`, generic pipeline): chunk_markdown (ATX split,
  800-char cap, <50-char fragment merge, section_path) → HashingEmbedder
  (blake2b feature hashing, CJK unigram+bigram + ASCII words, library-level IDF,
  sublinear tf, deterministic across processes — built-in `hash()` is forbidden)
  or OpenAICompatEmbedder (disk cache `blake2b(model\x00text).json`, zero network
  on hit) → VectorStore (cosine linear scan, <100 chunks so ANN is overkill) →
  rerank (0.6·cosine + 0.3·IDF-weighted Jaccard + 0.1·title hit). `runbook_search`
  tool supports keyword/vector/hybrid modes with identical output shape (top-3
  full documents, byte-identical in keyword mode); no-match is judged by *lexical
  evidence* (real feature intersection), never by absolute cosine — hash-bucket
  noise measured at 0.13+ cos beats weak true hits, bucket collisions cannot fake
  feature intersections. Default mode is `keyword`: eval_rag measured hybrid
  R@3 0.944 < keyword 0.972 (gate rule says fall back); hybrid wins R@1
  (0.889 vs 0.778) / MRR / literal+paraphrase, opt-in via
  `oncall.runbook_search`. Labeled set: `evals/rag_queries.json` (36 queries,
  literal/paraphrase); eval script: `scripts/eval_rag.py` (offline, zero key).
  Config keys `[rag]` (embedding_provider/base_url/model/api_key/cache_dir) —
  three-way sync in config.py dataclass + _apply_toml + _apply_env.
- **OnCallCopilot** (Phases 0–4 landed): duty workbench on the Web Console — alert
  queue (click → canned diagnosis goal), service health polling (3s `/api/state`),
  drill panel (POST `/api/inject|reset`), impact-aware approval cards, report overlay
  (`oncall/report.py` generated at run end; read via `/api/report/{name}`). Demo path:
  mock server has an oncall branch keyed on request tools (state machine reads env
  alerts; never scan full message text for tool names — goal text mentions them).
- **OnCallCopilot** (Phases 0–3 landed): eval suite `--suite oncall` records/replays
  20 fault scenarios with triple assertions (root-cause hit / exact mutation set /
  recovery-or-zero-mutation end state); CI gate `tests/integration/test_oncall_eval_replay.py`;
  harness preapproves tools (assertions replace approvals) while real approval flow is
  covered by `tests/integration/test_oncall_permission_flow.py`.
- **OnCallCopilot** (Phases 0–2 landed): SRE on-call domain layer on top of the
  runtime — file-based sim env (`oncall/sim.py`), 20 fault scenarios (`oncall/faults.py`,
  14 recoverable / 6 diagnosis-only), 9 domain tools incl. approval-gated mutations with
  exact action↔fault recovery semantics (`oncall/tools.py`), 9 runbooks, inject CLI
  (`python -m kyle_agent.oncall.inject`). Opt-in via `[oncall] enabled`. See
  docs/design/tech-design.md before touching `oncall/`; recovery rules must stay
  aligned with scenario expected_actions.
- **Web Console** (`src/kyle_agent/webui/`): FastAPI WS↔TCP relay + static
  single-page frontend (optional deps `[webui]`, entry `kyle-webui`). The bridge
  never parses or rewrites protocol messages; frontend has no build step.
  CI installs `--extra webui`; E2E coverage in `tests/integration/test_webui_bridge.py`.
  Design system v2 "control-room": neutral graphite dark, white is the only action
  color (primary buttons/focus ring/send), status colors are semantic only
  (green=healthy, amber=approval, red=alert). Light/dark themes: `:root` holds dark
  defaults, `:root[data-theme="light"]` overrides; all status tints derive via
  `color-mix(var(--ok/--warn/--err/--info))` so themes adapt automatically. Toggle in
  topbar persists to localStorage (`kyle.theme`), first visit follows
  `prefers-color-scheme`; an inline `<head>` script sets `data-theme` before CSS to
  avoid flash. Bridge serves `/` and `/static` with
  `Cache-Control: no-cache` (browsers must revalidate after asset changes).
  Frontend `mdRender` supports headings/lists/fences/links and pipe tables (report
  overlay wraps content in `.assistant-text` to reuse table styles).

Docs rule: every functional change updates this file and README together.


### Git network operations are auto-proxied (machine setup)

`~/bin/git` wraps the real git: `push/fetch/pull/ls-remote` auto-start
Clash Verge (D:/software/Clash Verge) when local proxy 7897 is down, then wait
until ready. Do NOT add `-c http.proxy=` bypasses — direct github.com access
is flaky on this machine; going through the proxy is the reliable path.
Generic wrapper for any command: `with-proxy.sh <cmd...>` (also in ~/bin).
Clash Verge is in the user Startup folder (proxy comes up at boot).

### Protocol layer (`src/kyle_agent/core/bus/`)

All IPC messages are typed pydantic v2 models with a **discriminated union on the `type` field**. This is the contract boundary — adding a new command or event means adding a new model class to `commands.py` or `events.py` and extending the `Command`/`Event` union.

- `envelope.py` — `JsonRpcRequest`, `JsonRpcSuccess`, `JsonRpcError`, error code constants, `make_error()`
- `commands.py` — `Command` union: ping, agent.run, session.*, permission.respond, event.subscribe
- `events.py` — `Event` union: run.*, step.*, tool.*, llm.*, permission.*, session.*, context.compacted, subagent.*, skill.invoked

`WIRE_PROTOCOL.md` is **generated** from these models by `scripts/gen_protocol_doc.py`. Always regenerate and commit it after changing bus models.

### Transport layer (`src/kyle_agent/core/transport/`)

- `socket_server.py` — TCP server (`asyncio.start_server`); reads NDJSON lines, dispatches to registered `CommandHandler`s, handles JSON-RPC error cases. On `start()`, probes `host:port` first — errors if another daemon is already listening. Handlers registered via `server.register("method.name", handler_fn)`.

### Config (`src/kyle_agent/core/config.py`)

Four-tier priority: **built-in defaults → `~/.kyle/config.toml` → `.env` → env vars**.

S0 keys: `host` (default `127.0.0.1`), `port` (default `7437`), `log_level`, `log_file`. Config file is silently skipped if absent; unknown keys cause a hard exit.

Relevant env vars: `KYLE_CONFIG`, `KYLE_HOST`, `KYLE_PORT`, `KYLE_LOG_LEVEL`, `KYLE_LOG_FILE`, `KYLE_LOG_FORMAT`.

### Daemon entry (`src/kyle_agent/core/app.py`)

`CoreApp.run()` is the single async entry point: loads config → sets up logging → creates `SocketServer` → registers handlers → waits for `SIGINT`/`SIGTERM` → calls `server.stop()`. Adding new handlers: instantiate a handler method on `CoreApp` and call `server.register()`.

### Testing

Integration tests in `tests/conftest.py` spawn a real daemon subprocess using a random free port (via `free_port` fixture). The fixture finds a free port, releases it, passes it to the daemon via `KYLE_PORT`, then polls `asyncio.open_connection` until the daemon is ready.

### Code style

All functions must have a **single-line Chinese comment** immediately above the `def` line explaining what the function does. Example:

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

Do not write multi-line docstrings; one concise Chinese line is enough.

**Test functions** require **two Chinese comment lines** immediately above the `def` line:

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

- `# 功能：` — 该测试验证的具体行为或不变式，一句话说清楚"测什么"
- `# 设计：` — 为什么选择这种测试方式：覆盖了什么边界条件、为什么用这个 stub/fixture、这种断言方式相比其他方式的优势

两行注释缺一不可。功能行让读者 5 秒内判断测试意图；设计行让读者理解测试背后的决策，而非只看到操作步骤。

### Design docs (outside the repo)

The planning documents live in `../docs/` (sibling of this repo, not committed here):
- `agent_development_plan.md` — staged development roadmap S0–S8
- `s0_implementation_plan.md` — detailed S0 decisions and rationale
- `agent_functional_outline.md` — full feature catalogue
