# sigil as a Hermes memory provider

Hermes can drive an agent from one memory backend. sigil can be that backend: `memory.provider:
sigil` in the profile's `config.yaml` makes recall arrive in the prompt on its own and turns
land in the vault without the agent being asked to use a CLI.

The failure this prevents is the one sigil was built around. Today sigil is a CLI an agent has to
be told to use, and the vault is only as good as the agent's discipline: one session writes
nothing, another dumps a transcript, and nothing tells you which. As a provider the recall
discipline is the default, and the per-turn append — the thing that turns a corpus into noise —
never happens at all.

## Install

sigil ships as an ordinary package that publishes a Hermes entry point, so nothing is copied
into the profile. The entry point lives in the distribution's `pyproject.toml`:

```toml
[project.entry-points."hermes_agent.memory_providers"]
sigil = "sigil:register"
```

Install it from a checkout, or from the wheel the release workflow attaches to each GitHub Release
(the asset is named `<distribution>-<version>-py3-none-any.whl`, e.g.
`sigil_memory-0.10.0-py3-none-any.whl`):

```bash
pip install -e .                              # from a clone of this repo
pip install ./sigil_memory-0.10.0-py3-none-any.whl   # or a downloaded release asset
```

Then select it in the profile's `config.yaml` — two lines, nothing else:

```yaml
memory:
  provider: sigil
```

That is the whole selection: the entry-point name (`sigil`) is the name Hermes matches against
`memory.provider`.

## Where the vault comes from

The provider binds its vault from `initialize()`, taking the first of these that is an existing
directory:

1. `$SIGIL_VAULT` (the pre-rename `$QUIPU_VAULT` still resolves) — an explicit path wins over
   everything else.
2. `<hermes_home>/sigil-vault` — the profile-scoped vault, e.g.
   `~/.hermes/profiles/glitch/sigil-vault`.
3. `~/sigil-vault` — the shared vault.

So if your vault is not `~/sigil-vault` and you do not want it under the profile, set
`SIGIL_VAULT` in the profile's environment:

```bash
export SIGIL_VAULT=/path/to/vault
```

Two knobs, both optional, both read when the provider is constructed: `memory.sigil.limit`
(default 5, entries per recall) and `memory.sigil.max_tokens` (default 400, the recall budget).
The env vars `SIGIL_PREFETCH_LIMIT` and `SIGIL_PREFETCH_MAX_TOKENS` override the same two values.
A pre-rename profile keeps working: `memory.quipu.*` and the `QUIPU_PREFETCH_*` env vars still load.

## What the agent sees

Two things, both small.

**A static system block** (`system_prompt_block()`): the vault path, the entry count and how many
are promoted, and the discipline — recall before answering, write after a real outcome. It
carries no recalled entries, only the counts, and it is small: 27 words on an empty vault in a
real run, against a 150-word ceiling the contract test holds it to.

**A recall block per turn** (`prefetch()`), built from the SQLite FTS index and capped at the
token budget: a heading and one line per hit, `- [id] summary (state, score)`. The cap is
enforced on characters — budget × 4 — and the cut is made at the last line break inside the
budget, so a recall list is not left ending in half a line. A query shorter than 4 characters, or
one that matches nothing, returns nothing at all.

The budget is the point. A memory provider that floods the prompt is worse than no provider: the
agent reads the noise instead of the question. Recall is also **not** an LLM call and makes **no**
network request — it is BM25 over the local index, so it costs milliseconds and works with the
network unplugged.

## What gets written, and what deliberately does not

`prefetch()` reads. `sync_turn()` writes to exactly one place:

```
<vault>/.sigil/session-<session-id>.jsonl
```

One JSON line per turn, with the timestamp, the session id and the user and assistant text (each
side truncated to 4000 characters). That file is a transcript, not a memory. **No vault entry is
created per turn** — the entry count does not move when a session runs, and the vault does not
fill with `session: "fixed a bug"` rows. This is the append-only failure sigil exists to fix,
and the provider refuses to commit it.

Entries come from a promotion pass instead: `sigil reflect` over a transcript, or `sigil hook` at
a session end. The gate is the same one the CLI applies — importance over the threshold **and**
evidence present — and a summary that is a topic rather than a statement does not clear it. See
[USING.md](USING.md) for the gate and the reflection pass.

The provider adds two tools for an agent that wants to reach further than the recall budget:

| tool | what it takes | what it does |
|---|---|---|
| `sigil_search` | `query`, optional `limit` (default 5) | BM25 search over the vault index; returns id, summary, state, score |
| `sigil_write` | `summary` and `evidence` (both required), optional `ask`, `kind` (default `fact`), `project`, `agent`, `importance`, `state`, `edges`, `supersedes` | writes one entry into the vault, reindexes it, and returns the `state` it actually wrote |

Call `sigil_write` after a real outcome — a decision with its reason, a measurement, a root cause
— and not per turn. The summary is the retrieval handle, so write it as a statement; the evidence
is what a skeptic would re-run.

An entry is promoted on the same grounds as any other: evidence **and** `importance` at or above the
vault's `promote_threshold` (0.5 by default). Omit `importance` and the entry stays `raw` — kept and
searchable, but not exported as training data. `state: promoted` cannot override the gate; asking for
it without the grounds writes `raw` and says so in a `note` field rather than failing the write.
`state: pruned` is honoured as-is, since pruning is a deliberate act rather than a gate question.

`edges` takes `src:predicate:dst` strings and `supersedes` takes entry ids. Predicates are a closed
vocabulary — `causes`, `depends_on`, `implements`, `supersedes`, `decided_against`, `measured_as`,
`located_in`, `used_by`, `blocks`, `owned_by`, `part_of`, `contradicts`, `learned_from` — and an invented
one is reported in `skipped_edges` rather than written. A malformed spec costs the claim, never the
memory: the entry is still written, reindexed, and returned.

## Measuring it

Turn the provider on, ask the agent something the vault already knows, and look at two places.

The vault side, from any shell:

```bash
sigil --vault ~/sigil-vault stats
```

That prints the corpus report as JSON — `entries`, `promoted`, `promotion_rate` and the export
counts. A vault the provider can search has a non-zero `entries`.

The session side, in the vault:

```bash
ls ~/sigil-vault/.sigil/session-*.jsonl
```

One file per session id, one JSON line per turn. It proves the provider is capturing turns. It
does **not** prove recall worked — for that the agent has to answer a question from the vault,
and the recall block has to appear in the turn's context.

## When it goes wrong

**The provider does not load.** Check that the entry point is installed:

```bash
python -c "import importlib.metadata as m; print([e.name for e in m.entry_points(group='hermes_agent.memory_providers')])"
```

`sigil` in that list means the distribution is installed where Hermes can see it; an empty list
means it is not — Hermes discovers a pip-installed provider by entry point, so a provider that
imports but was installed elsewhere is invisible.

**No vault found.** The provider says so instead of starting blind: `sigil: no vault directory
found (looked for: ...)`, naming the paths it tried, in order. Create the vault with
`sigil --vault <path> init`, or point `SIGIL_VAULT` at an existing one.

**An empty vault.** Not an error: recall returns nothing and the agent has no prior context.
`sigil stats` reports `entries: 0`.

**A path that is not a vault is refused.** A directory with no `notes/`, `entities/` and no `.sigil/`
(or `.quipu/`) is not a vault, and `sigil` will neither read from it nor write into it: the command
exits 2, names the path it tried, and creates nothing (#141). That is deliberate — opening a typo'd
path used to *create* a vault there and answer `[]`, which reads exactly like "no memory about this".
`sigil --vault <path> init` is the way to make a new one.

**`SIGIL_VAULT` pointing somewhere else.** The provider takes the first candidate that is a
directory, and `SIGIL_VAULT` is first — so an exported path silently wins over the profile vault.
The vault path is in the system block, so it is visible in the agent's own context.

**A broken vault degrades, it does not crash.** The provider never raises into the agent loop:
recall returns an empty string, the write tools return a JSON `error`, and turn capture is
dropped with a debug log. A session with a broken vault degrades to no recall; it does not die.

**A stale index.** The index is derived from the markdown. If entries were written outside the
provider (`sigil write`, a file edit), reindex before blaming the provider:
`sigil --vault ~/sigil-vault reindex`.

## See also

[USING.md](USING.md) — the CLI path: the recall-first habit, the promotion gate, corrections.
[DESIGN.md](DESIGN.md) — the layers and why `ask` is required.
