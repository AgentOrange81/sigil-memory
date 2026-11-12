# Architecture

## Modules

| file | responsibility | notes |
|---|---|---|
| `sigil/config.py` | vault paths, predicate vocabulary, gate threshold, LLM endpoint | `tomllib`, no deps |
| `sigil/store.py` | `Entry` model, frontmatter read/write, iteration | hand-rolled frontmatter: no PyYAML needed, lists are JSON arrays (valid YAML inline lists) |
| `sigil/index.py` | SQLite schema, FTS5 rebuild, search, stats | the db is disposable |
| `sigil/graph.py` | entities, temporal edges, timeline, neighbours, hub materialisation | closed predicate vocabulary; canonical names |
| `sigil/promote.py` | capture, reflection, proposal parsing, the gate, provider adapters | `FakeProvider` for tests/CI, `HttpProvider` for any OpenAI-compatible endpoint |
| `sigil/export.py` | SFT / preference / graph export, dataset report | provenance in every record |
| `sigil/cli.py` | argparse surface | no click/typer dependency |

Runtime dependencies: **none** (stdlib only). Dev: pytest + ruff.

## Data model

Entry frontmatter:

| field | type | meaning |
|---|---|---|
| `id` | 8 hex | stable identity; the filename prefix |
| `kind` | enum | capture, conclusion, decision, lesson, fact, handoff, question |
| `state` | enum | raw, promoted, pruned |
| `agent` | str | which profile/persona wrote it (multi-agent sharing) |
| `project` | str | retrieval scope |
| `session` | str | provenance back to the transcript |
| `created` / `updated` | ISO 8601 Z | |
| `confidence` | 0–1 | the model's own confidence |
| `importance` | 0–1 | the reflection pass's weight; drives the gate |
| `source` | str | transcript path, `cli`, `capture` |
| `ask` | str | the request this answers — the prompt half of a training example |
| `summary` | str | one complete statement (not a topic) |
| `evidence` | str | the proof: number, command, path, log line |
| `supersedes` | list[id] | earlier entries this corrects |
| `tags` | list[str] | |

SQLite schema: `entries` (mirror), `entries_fts` (FTS5, porter+unicode61, BM25), `entities`,
`edges` (temporal), `events` (append-only audit trail of assert/invalidate).

## Retrieval

Today: `search()` builds a quoted-token AND query with a prefix on the last token, filters on
kind/state/agent/project, ranks by `bm25()`, returns a `snippet()` per hit. That is deliberately
boring: FTS5 over 1k entries is sub-millisecond and needs no model.

Planned (see PLAN.md): hybrid scoring — BM25 + embedding cosine + a graph-proximity bonus — with the
graph bonus computed by expanding `neighbors()` from the matched entries' entities. MemPalace's
measured 98.4% R@5 comes from exactly that shape (semantic + keyword boosting + temporal proximity
boosting) before any reranker, which is the target to beat honestly on LongMemEval.

## Extension points

- **Provider**: `complete(prompt) -> str`. Any HTTP endpoint, or your own class with that one method.
- **Predicates**: `predicates` in `.sigil/config.toml`.
- **Promotion policy**: replace `promote.apply()`; the gate is one expression on purpose.
- **Exporters**: `export.py` functions each take `(cfg, out)`; a new dataset is a new function.

## Testing

`pytest` — 34 tests covering store round-trip (including unicode and quote/colon torture cases),
index search and filters, FTS prefix behaviour, temporal semantics (invalidate keeps history,
time-travel queries, contradiction), the promotion gate (threshold AND evidence), graph-only-from-
promoted, export schemas (SFT, preference pairs with provenance, graph files), and CLI end-to-end.
CI runs ruff + pytest on Python 3.10 / 3.12 / 3.13.

The `FakeProvider` exists so the whole pipeline — including reflection, promotion and export — can be
tested with no model and no network. Every test that involves an LLM goes through it.
