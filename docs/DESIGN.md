# Design

## 1. The problem, stated precisely

An agent that runs for months accumulates three kinds of knowledge, and each needs a different
mechanism:

| kind | example | right mechanism |
|---|---|---|
| **episodic** | "we tried fp16 VAE on gfx1031 and the ring hung" | cheap capture, kept verbatim |
| **semantic** | "gfx1031 needs `--cpu-vae` for heavy decodes" | promotion: distilled, always-loadable |
| **relational** | "sigil depends_on sqlite; sqlite used_by fts5" | graph edges, with validity windows |

Most systems store all three the same way — one vector table, one chunk size — and then wonder why
retrieval feels like guessing. sigil separates the storage per kind while keeping one query surface.

## 2. Three layers

```
notes/<id>-<slug>.md     the entries: episodic + semantic, frontmatter + body      <- source of truth
.sigil/index.db          FTS5 index over entries; entities; temporal edges         <- derived, disposable
entities/<slug>.md       graph hubs, written from the db                          <- browsable mirror
```

A vault written before the rename keeps `.quipu/` instead and it is read there, never moved. The db can
be deleted at any time; `sigil reindex` recreates the FTS index and `sigil edge` replays
rebuild the graph from the entries' own frontmatter if you keep edge assertions in them. Nothing in
the design requires the db to be the truth.

## 3. The lifecycle

States are explicit because they are training labels, not bookkeeping:

- **raw** — captured. Zero cost, zero trust. Never enters the graph, never exports.
- **promoted** — survived the gate. Enters the graph, exports as SFT, is eligible to be superseded.
- **pruned** — was promoted, later judged wrong or superseded. With `--supersedes` on the replacement,
  the pair becomes a preference example.

The gate is deliberately dumb and inspectable:

```python
promoted = importance >= cfg.promote_threshold and bool(evidence)
```

`importance` comes from the reflection pass; `evidence` is a required field the model must fill with
something a skeptic could re-check (a number, a command, a path, a log line). A model that cannot
produce evidence has not produced a memory.

## 4. Why the ask matters

A distilled fact is only half a training example. Without the question, `sigil` would be exporting
answers to questions nobody asked — which teaches a model to volunteer facts. So every promoted entry
carries `ask`: the request it answers, self-contained. Export filters asks shorter than 20 characters
or under three words, because "did that fix it" is a conversation turn, not a prompt.

## 5. Temporal edges, not deletion

```sql
edges(id, src, predicate, dst, note_id, confidence, valid_from, valid_to)
```

- `assert` opens an edge (`valid_to IS NULL`).
- `invalidate` closes it at an instant. The row stays.
- `contradict` closes the old and opens the new in one transaction.
- `timeline(entity, at=...)` answers "what did we believe in June".

Deleting an edge destroys the most valuable record in the system: that we used to think otherwise.
MemPalace reached the same conclusion with its `invalidate + add at one instant` normalisation; we
take the mechanism and add the export that falls out of it.

## 6. What was taken from where

Only what has been verified against source (this table grows as the survey continues):

| system | what we took | what we deliberately did not |
|---|---|---|
| **Icarus / fabric** (the system this replaces) | the entry taxonomy (decision/lesson/resolution/handoff), the idea that an entry needs a summary that is a statement | auto-append at session end; an open predicate space; a flat corpus with no lifecycle |
| **MemPalace** | temporal entity-relationship graph with validity windows; closed predicate vocabulary normalised by invalidate+add; a cheap `audit` pass that scores the corpus and prints a repair list; hub files browsable as markdown | verbatim-only storage (we need distillation for training), ChromaDB as a required dependency |
| **Honcho** | the "deriver" pattern: an asynchronous background pass that reads messages and maintains a representation, so recall is not a per-request LLM call; peer/session separation (our `agent` / `session` fields) | a Postgres service and AGPL coupling; theory-of-mind representations are out of scope for v1 |
| **self-evolving memory (Cole Medin's KB)** | promotion instead of appending; a reflection pass run in an isolated context so it does not pollute the working one; the dual posture of a hand-curated rules file next to autonomous memory | treating the promoted file as the only artifact — we keep the raw layer and the graph |

Surveyed at source level on 2026-10-28 (repos cloned and read, not READMEs). What each one taught:

| system | what we took | what we skipped, and why |
|---|---|---|
| **mem0** (Apache-2.0) | additive extraction with `linked_memory_ids`; entity boost that is *penalised* by hub degree, `sim*0.5*1/(1+0.001(n_linked-1)^2)` — a node touching everything is not evidence | its hybrid retrieval needs a `keyword_search` backend or it silently degrades to vector-only; the knowledge graph was **removed from OSS in v3** (Platform-only) despite the paper |
| **Graphiti / Zep** (Apache-2.0) | genuinely bi-temporal edges (`valid_at`/`invalid_at`/`expired_at`); contradiction resolved by **date arithmetic, no LLM verdict**; MinHash/LSH blocking (32 perms, band 4, Jaccard 0.9) before any LLM dedupe | requires a graph database as the source of truth — the opposite of markdown-first |
| **Letta** (Apache-2.0) | git-backed memory files with commit-time frontmatter validation (`description` required, `read_only` immutable, size/depth caps) — a pre-commit hook as the quality gate | memory blocks and archival vector memory **no longer exist** in current source; no graph at all |
| **Cognee** (Apache-2.0) | `uuid5(normalized identity)` so re-ingest merges instead of duplicating; a three-stage relation vocabulary pass | 20+ search types and a "truth subspace" are complexity we cannot justify at 1k notes; extraction is not practically local |
| **Memori** (Apache-2.0) | a plain SQL SPO triple table with `UNIQUE(entity,s,p,o)` and a reinforcement counter — proof the graph does not need a graph DB | no temporal validity, only a hit counter |
| **Basic Memory** (AGPL-3.0) | the closest analog to our vault: markdown source of truth, derived SQLite/FTS5, **no LLM in indexing**; authored bi-temporality `@effective[from,to)` with fixed-width bounds so string compare is chronological; fusion `max(v,f) + 0.3*min(v,f)` | AGPL — reimplement the ideas, never vendor the code |
| **Honcho** (AGPL-3.0) | the deriver prompt discipline: atomic facts about a target, mandatory `source_indices` back to raw messages, resolve relative time against message time, never derive facts about A from B's messages; `observer`/`observed` on every document | Postgres+pgvector service; belief-graph/"what does A know about B" is out of scope for v1 |

**Ranked, the five mechanics worth having** (all cheap, all LLM-optional):

1. Deterministic identity — `uuid5`-style key over a normalised name. Kills re-ingest duplicates and
   four-profile merge races at zero cost. (Landing in this repo now: `identity()`/`resolve()`.)
2. Authored `@effective[a,b)` in the markdown itself, indexed as scalars — bi-temporality that
   survives a reindex because the truth is in the file.
3. Additive extraction that carries `source_indices` — "no destructive rewrite", and (evidence → fact)
   is exactly the shape of an SFT pair.
4. Dated contradiction invalidation with no LLM verdict — "facts changed" becomes a `WHERE` clause.
5. FTS5/vector fusion with a title boost and a gate, plus hub-penalised entity boost. CPU-friendly, no
   reranker needed.

**Traps this survey saved us from:** mem0's OSS build has no graph; both closest fits (Basic Memory,
Honcho) are AGPL; Letta's memory blocks are gone from current source; Graphiti's RRF is `Σ1/(rank+1)`,
not the 1/60 convention, so its published numbers are not comparable to systems that use 1/60.

## 7. Failure modes this design is a reaction to

Each of these cost real time in the corpus sigil was extracted from:

1. **84% noise.** Auto-writing a `session` entry at every session end put 4,572 session rows in front
   of 855 real ones. Fixed by the gate — and by making raw captures free but promotion expensive.
2. **A silent hook failure.** A `KeyError` in the scoring function killed every session-end write for
   months; the only symptom was a one-line warning. Hence `sigil audit`, and hence tests that assert
   *an entry was written*, not that a call returned.
3. **Evidence-free entries.** A plausible summary with no proof is worse than nothing: it gets
   retrieved and trusted. Hence `evidence` is required for promotion and audited.
4. **Entity twins.** "DOCP 3200", "docp-3200", "do cp-3200" in three nodes. Hence canonicalisation at
   write time with the observed spelling kept as an alias.
5. **Encoding damage.** JSON `\uXXXX` escapes and lone surrogates have already cost this project one
   migration. The store writes UTF-8 strictly and tests unicode round-trips.
