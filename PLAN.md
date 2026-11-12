# Plan

Milestones are ordered by "what unblocks Dirk's use today", not by what is most fun to build.

## M0 — v0.1: the lifecycle (done, this commit)

- [x] `Entry` model + markdown store (no YAML dep), unicode-safe round-trip
- [x] SQLite FTS5 index, BM25 search, filters
- [x] temporal graph: assert / invalidate / contradict / timeline / neighbours, canonical names
- [x] promotion gate (importance + evidence), reflection pass with `FakeProvider` + `HttpProvider`
- [x] exports: SFT JSONL, preference pairs, graph nodes/edges
- [x] `audit`, `stats`, CLI end-to-end, 34 tests, ruff clean, CI

## M1 — make it survive contact with our own corpus

- [ ] **Import an existing Icarus vault.** `quipu import fabric <dir>`: map fabric entries to
  `Entry` (kind from `type`, `state` from `training_value`, `ask` from the entry body if it quotes the
  request), drop `session` stubs into `state=pruned` with a reason, keep provenance in `source`.
  Test against a 20-entry fixture, then run it on the real 5,469-entry archive.
- [ ] **Reflection over a real Hermes session file**, end to end, with the local model:
  `quipu reflect ~/.hermes/profiles/*/sessions/<id>.jsonl`.
- [ ] **`quipu hook`** — a thin `on_session_end` adapter so an agent framework can call the reflection
  pass without embedding quipu: reads a transcript path from argv/env, runs `reflect`, prints a
  one-line summary, exits non-zero on failure so the failure is visible (the Icarus lesson: a silent
  hook is a dead hook).
- [ ] **Dedup on promotion**: before writing, check near-duplicates (minhash over the body, threshold
  0.8) and turn a duplicate into an `updated` timestamp bump rather than a new entry.

## M1.5 — integration seams (how agents actually use this)

Three surfaces, cheapest first, and none of them makes an agent framework the owner of the memory.

- [ ] **Skill only (shipped, zero code).** The `quipu` skill tells an agent when to recall and when to
  write, using the CLI. Framework-agnostic, breaks nothing when the internals move. This is the
  supported path today.
- [ ] **`quipu mcp` — stdio MCP server.** Expose `search`, `write`, `timeline`, `neighbors` over MCP so
  any client (Claude Code, Cursor, Codex, Hermes' native MCP client) can use the vault. Thin wrapper
  over the same `quipu` functions; no second implementation. This is the portability story.
- [ ] **Hermes MemoryProvider, packaged.** Ship `quipu.hermes_provider` with a
  `hermes_agent.memory_providers` entry point and `register(ctx)`, so `memory.provider: quipu` gives:
  `prefetch()` → automatic top-k recall before a turn, `sync_turn()` → cheap per-turn buffering,
  `system_prompt_block()` → a small always-loaded digest, `on_pre_compress()` → memory injected before
  context is compressed, `get_tool_schemas()`/`handle_tool_call()` → search/write as native tools.
  This is Icarus' feature set through a **documented contract**, shipped inside quipu's own package —
  no directory forked into `~/.hermes/plugins`, no version to chase.

**Why not a bespoke Hermes plugin.** Icarus was a plugin *and* the only integration path, so when
upstream stopped committing, the corpus could only be reached through abandoned code. A CLI contract
plus an optional provider plus an optional MCP server means Hermes is a *consumer* of quipu: if the
provider contract changes, we lose auto-injection, not the memory.

**`sync_turn` must not write an entry per turn.** That is exactly the append-only failure this project
exists to fix. Each turn appends to `.quipu/session-<id>.jsonl`; one reflection pass at session end
(issue #3's hook) turns the buffer into gated entries. Capture is cheap, promotion stays expensive.

Measured inputs for M2–M4 (hardware, models, costs, yields): [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).

## M2 — retrieval that is actually comparable

- [ ] **Embeddings, optional**: `quipu embed --model <name>` writing a `vec` table; cosine in Python
  (1k–50k notes is fine in numpy) or sqlite-vec if it is worth the dependency. **Default to
  bge-small-en-v1.5 (384d, MIT)**: measured 117/s on GPU and 81/s on CPU, so 50k notes re-indexes in
  ~7–10 min without a GPU at all.
- [ ] **Hybrid query**: `score = w_bm25*bm25 + w_vec*cos + w_graph*graph_bonus`, weights in config.
- [ ] **Benchmark harness** against LongMemEval and LoCoMo with our honest R@5/R@10 numbers published
  in `benchmarks/`, including the failures. No "100%" headline.
- [ ] **Graph expansion at query time**: `search --graph-depth 1` pulls in edges of matched entities so
  "why did we drop DOCP" can surface the correction even when the words differ.

## M3 — the graph gets built for us

- [ ] **Relation extraction pass over promoted entries** (the `edges` field already exists; today it
  comes from the same reflection call — M3 makes it a second, cheaper pass over the whole corpus).
- [ ] **Community summaries**: cluster the entity graph (label propagation, no dependency) and write
  one summary per cluster as a `conclusion` entry. GraphRAG's idea at 1/1000th the cost.
- [ ] **Contradiction detector**: a pass that finds two live edges that cannot both be true and files
  a `question` entry for a human to resolve. The resolution is then a preference pair for free.

## M4 — training data as a product

- [ ] **`quipu export` quality gates**: minhash dedup, length/diversity filters, decontamination against
  a held-out eval set, and a `dataset card` (counts, hashes of source entries, builder version).
- [ ] **Rebuildable datasets**: `quipu export --manifest out.json` records every contributing entry id +
  hash, so a dataset can be reproduced from a vault commit.
- [ ] **DPO mining beyond supersedes**: promoted-vs-raw pairs (the reflection chose one and rejected the
  other) are the second free source of preference data.
- [ ] **First real fine-tune** on a local model with the exported set, evaluated against the same
  questions answered without memory.

## M5 — multi-agent

- [ ] **Shared vault, per-agent scoping**: `agent` is already a field; add `--agent` isolation rules
  (what a peer may read), and a `quipu peer` note type for handoffs.
- [ ] **`quipu mcp`**: expose search/write/timeline as MCP tools so any agent framework can use it
  without a bespoke plugin (the reason Icarus was fragile was that it *was* the plugin).
- [ ] **Hermes plugin**: a ~150-line plugin that calls the CLI, or nothing at all if the CLI + a skill
  is enough. Prefer the latter.

## Non-goals

- A hosted service, a dashboard, a vector DB of our own, or an agent framework.
- Theory-of-mind representations (Honcho's dialectic). Interesting, not needed to answer "what did we
  decide and why".
- Summarising everything. The raw layer stays raw and cheap.
