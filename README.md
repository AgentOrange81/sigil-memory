# sigil

**Local-first agent memory that knows what it changed its mind about.**

A sigil is a mark that binds intent. This one binds your agent's memory to plain markdown: the notes are
the truth, the index and the graph are derived and disposable, and — the part nothing else ships — the
memory's own lifecycle becomes labelled training data.

```bash
pip install -e ".[dev]"        # or: uv sync
# or the released wheel, without a checkout:
#   pip install https://github.com/AgentOrange81/sigil-memory/releases/latest/download/<wheel>
sigil --version
sigil --vault ~/memory init
sigil --vault ~/memory capture "the 3600 DOCP config threw 649 CCEs in one boot" --project wraith
sigil --vault ~/memory write --kind decision --state promoted \
  --summary "DOCP 3200 with FCLK 1600 is stable on the TRX40" \
  --evidence "649 MC25_STATUS CECC events in boot -1; 0 since" \
  --ask "why did the box reboot at 03:02?" --project wraith
sigil --vault ~/memory search DOCP --project wraith
sigil --vault ~/memory export sft --out data/sft.jsonl     # supervised examples
sigil --vault ~/memory export dpo --out data/dpo.jsonl     # preference pairs
```

`--vault` is a global flag: it goes **before** the subcommand. It defaults to the working
directory, so inside a vault you can leave it out entirely.

`sigil reflect <transcript>` is the other way in, and it is the only one that needs a model: it posts
the transcript to the OpenAI-compatible endpoint in `.sigil/config.toml` (`[llm] base_url`, default
`http://127.0.0.1:8080/v1`). With no endpoint reachable it fails loudly with
`RuntimeError: LLM request failed: <urlopen error [Errno 111] Connection refused>`; use
`sigil reflect <transcript> --fake <file.json>` to supply the proposals yourself and skip the
network entirely.

No model or network is needed to see the whole lifecycle — capture, reflect, promote, correct,
export:

```bash
bash examples/demo.sh
```

That script builds a throwaway vault, runs a reflection pass against a canned response
(`sigil reflect <transcript> --fake <file.json>`), writes a correction with `--supersedes`, and
exports both datasets.
**After the quickstart, read [docs/USING.md](docs/USING.md)** — it is the day-to-day guide: the
recall-first habit, what makes an entry worth promoting, how to correct one, and what to do when
something goes wrong.

## Why another memory system

Three things go wrong in every "agent memory" I have run in anger:

1. **It appends instead of promoting.** Auto-writes at session end fill the corpus with
   `session: "fixed a bug"` rows. In the corpus this project was extracted from, 84% of 5,469 entries
   were `session` noise and 73% were tagged `low` — 4,049 rows that made retrieval worse, not better.
2. **It forgets that it forgot.** A note that says "DOCP 3600 is stable" is indistinguishable from one
   that says "DOCP 3200 is stable" unless the graph keeps *when* each was true and *why* it changed.
3. **It exports a pile, not a dataset.** A flat dump of entries is not fine-tuning data: no prompts,
   no negatives, no provenance, no way to rebuild the dataset from the corpus.

sigil is built around the inverse of each:

| Problem | sigil |
|---|---|
| append-only noise | **capture → reflect → promote** ladder; raw captures are cheap and stay raw until a gated reflection pass promotes them |
| memory that silently goes stale | **temporal edges** (`valid_from` / `valid_to`); contradiction is an operation, not an edit. History survives |
| unusable for training | **lifecycle labels**: promoted → positive example, pruned → negative, superseded → preference pair. Export with provenance |

## The thesis: the lifecycle *is* the label

Most systems throw away the most valuable signal they produce. When memory decides something mattered,
it has labelled that entry. When it later corrects itself, it has produced a preference pair.

```
capture ──(reflect)──► proposal ──(gate: importance ≥ 0.5 AND evidence)──► promoted
                          │                                                 │
                          └──────── below gate ──────────────► raw          └─(superseded by a later entry)
                                                                                └─► DPO pair: stale ≺ corrected
```

`sigil export dpo` emits exactly that:

```json
{"prompt": "can I run DOCP 3600 on the TRX40 after the BIOS flash?",
 "chosen": "DOCP 3600 with FCLK 1800 is stable after BIOS 2601 — 3 boots, zero MCEs.\n\nEvidence: 3 consecutive boots, journalctl -k per boot: 0 MCE lines each",
 "rejected": "DOCP was dropped from 3600 to 3200 with FCLK 1600 to resolve instability.\n\nEvidence: 649 CCEs in boot -1",
 "meta": {"sigil": {"entry": "…", "kind": "decision", "state": "promoted"},
          "superseded": {"entry": "…", "kind": "decision", "state": "promoted"},
          "reason": "3 consecutive boots, journalctl -k per boot: 0 MCE lines each"}}
```

That record is a real one, produced by the tool from a real correction. Note what it carries: the
question that was actually asked, both answers, the evidence that decided it, and the ids of both
entries — so the dataset can be rebuilt from the vault. A preference pair appears only where an
entry corrects another, so `export dpo` on a fresh vault returns zero pairs.

## Design rules

- **Markdown is the source of truth.** The vault is an Obsidian folder. SQLite is a disposable index
  (`sigil reindex` rebuilds it in milliseconds); delete the db and you lose nothing.
- **Evidence or it did not happen.** A promoted entry without evidence is a bug, and `sigil audit` says so.
- **Closed predicate vocabulary.** An open predicate space becomes 400 spellings of "uses". New
  predicates go in the config, deliberately.
- **One entity, one spelling.** Names are canonicalised on write; the spelling seen becomes an alias.
- **The graph learns only from promoted entries.** A raw capture is allowed to be wrong.
- **Reflection runs isolated.** It is its own process with its own context — it never pollutes the
  session it is reviewing, and it runs while the context is still fresh.
- **No vendor, no daemon, no lock-in.** One Python package, stdlib only at runtime, any
  OpenAI-compatible endpoint for the reflection pass.

## Commands

| command | what it does |
|---|---|
| `sigil init` | create a vault |
| `sigil capture` | store a raw capture (cheap, unfiltered) |
| `sigil reflect <transcript>` | one reflection pass → proposals → gated writes |
| `sigil write` | write a curated entry directly (with `--supersedes`) |
| `sigil import fabric <dir>` | import an Icarus/fabric corpus into the vault (top-level `*.md` only — #17) |
| `sigil search` | BM25 over entries, filterable by kind/state/agent/project |
| `sigil show` | print an entry |
| `sigil edge` / `invalidate` / `contradict` | assert and retract graph edges |
| `sigil timeline <entity> [--at ISO]` | what was true, and when |
| `sigil neighbors <entity> --depth 2` | graph neighbourhood |
| `sigil export sft\|dpo\|graph` | write a dataset |
| `sigil audit` | find corpus problems (promoted-without-evidence, dangling supersedes) |
| `sigil stats` | promotion rate, graph size, dataset counts |

The importer maps a fabric entry's `type` to a `kind`, its `training_value` to a `state`
(`low` arrives as `pruned`, never as promoted), and joins `outcome` and `evidence` into one
evidence line. It reads `*.md` at the top level of the source directory only — nested
subdirectories are not walked, and the importer does not yet report why a file was skipped
(#17). The MCP server is not in `main` yet; see "What this is not".

## What this is not

- Not a vector database. Retrieval is BM25 (SQLite FTS5) today; hybrid scoring + embeddings is
  planned, not in `main` (#5).
- It **is** an MCP server: `sigil mcp` serves `search`/`write`/`timeline`/`neighbors`/`stats` over stdio.
- It **is** a Hermes memory provider: the package installs as a plugin directory (`hermes memory status`
  shows `installed ✓ / available ✓`) and adds pre-turn recall, per-turn buffering and a digest.
- Not a hosted service. Two processes maximum: you and the LLM you point it at.
- Not a finished product. It is a working v0.1 with tests, built to be developed in the open.

## Status

v0.1.0 — core lifecycle, temporal graph, three exports, and the fabric corpus importer are
implemented and tested (70 tests). The suite and the contract it enforces live in `tests/`;
the promotion gate and the lifecycle are unchanged from v0.1.0. CI runs on Python 3.10, 3.12 and
3.13; `test (3.10)`, `test (3.12)` and `test (3.13)` are required status checks on `main`, and the
checks are enforced for admins too — a green matrix is what merges. `main` is protected:
squash-merge only, linear history, no force-push, no branch deletion. The contribution workflow is
in [CONTRIBUTING.md](CONTRIBUTING.md); the priorities are in [PLAN.md](PLAN.md).

MIT licensed.
