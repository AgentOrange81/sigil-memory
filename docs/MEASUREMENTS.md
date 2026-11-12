# Measurements

Numbers taken on this project's own hardware and corpus, not estimates. Method and any caveats are
recorded next to each one, because a figure without its method is a rumour.

Box: Threadripper/3950X-class CPU, AMD GPU (gfx1032, 8 GB) with no CUDA, 30 GiB RAM, 822-note vault.
Full research write-up (GraphRAG/LightRAG/A-MEM source reading, prompt shapes, per-stage cost):
`~/agent-memory-graph-ft-spec.md` on the author's box; the durable claims are repeated here.

## Graph extraction cost (the number that decides the architecture)

| stage | model | cost for 822 notes |
|---|---|---|
| entity+relation extraction | Qwen3-4B-Instruct Q4 | **15.9 s/note → ~3.6 h** |
| entity+relation extraction | Qwen3.6-35B-A3B (the local default) | ~140 s/note → **27–36 h** |
| entity description summarisation | 35B | +10–25 h |
| community reports (GraphRAG's Leiden pass) | 35B | **60–100 h** |

Whole GraphRAG stack for this corpus is ~14 M input / 2.7 M output tokens. On a mini model that is a
few dollars — **local-vs-cloud was the wrong axis**; the decision is which stages run at all. A 4B
model makes the extraction stage an overnight job instead of a week, so M3 targets extraction only
and skips community reports until retrieval proves it needs them.

### Failure modes measured while building the extraction prototype

- **Thinking tokens ate the whole budget**: a reasoning model given 900 output tokens returned 0
  parsable records after 139 s. Send `enable_thinking: false` (or the model's equivalent).
- **Zero relationships, repeatedly**: 3 of 4 runs produced 11–19 entities and **no** relations with
  `finish=length`, including after raising the budget to 1700 tokens on a list-shaped note (38
  entities, still 0 relations). A bigger budget is not the fix — assert edge yield per run and fail
  the batch when it is zero.
- **A 4B model drops the record-type prefix** GraphRAG's parser requires (`"entity"<|>NAME<|>...`).
  Our own prompt asks for JSON, so this is only a trap for anyone copying GraphRAG's delimiter format.
- **Default entity types mislabel technical content**: GraphRAG's `PERSON/ORGANIZATION/GEO` typed
  Next.js, Prisma and Redis as ORGANIZATION. A domain taxonomy
  (`PROJECT/TOOL/HOST/SERVICE/FAILURE/CONCEPT`) gave correct types on the same notes.

## Embeddings (measured on the 8 GB AMD card via Vulkan, and on CPU)

| model | dims | licence | GPU | CPU | 50k notes |
|---|---|---|---|---|---|
| **bge-small-en-v1.5** | 384 | MIT | **117/s** | 81/s | **7 min** (CPU: 10 min) |
| nomic-embed-text | 768 | Apache-2.0 | 32.5/s | — | 26 min |
| Qwen3-Embedding-0.6B | 1024 | Apache-2.0 | 8.9/s | 4.9/s | 1.6 h |

Dimension is not the constraint and the GPU barely matters for the small models. **bge-small is the
default for M2**: MIT, fast enough that a full re-index is a coffee break, and a 384-dim table keeps
the SQLite index small. Quality ranking on *this* corpus is unmeasured — no MTEB run — so that choice
is provisional until the benchmark harness (issue #6) can compare retrieval, not embeddings.

Caveat: those throughputs are Vulkan; ROCm on gfx1032 was not exercised for this (no torch in the
harness at the time). Treat the CPU column as the floor, not the ceiling.

## Training-data yield from this corpus

| gate | survivors of 667 evidence-bearing entries | note |
|---|---|---|
| lexical "has evidence" | 655 (98%) | **useless as a selector** — everything passes it |
| structural (kind + self-contained ask) | 484 | decisions only; loses lessons and facts |
| realistic candidate pool | **563** | after noise scrubbing |
| expected after a semantic judge | **225–340** | one LLM pass scoring answer-worthiness |

The lesson for `export.py`: a lexical gate on "has evidence" is decoration. The selector has to be
structural plus semantic, and the number to publish is what survives *all* of them.

### Split order (reversed from the obvious)

`cluster → split → derive → gate → decontaminate`, at **cluster level**, stratified by project and by
`agent`+month where the project is unknown, 80/10/10. With 822 notes that is ~658/82/82 — enough to
catch regressions and leakage, **not** enough to claim a statistically meaningful delta from a 4B
model. Say so in any result, or the number is a story.

## What was NOT verified

- ROCm on gfx1032 for embedding throughput (Vulkan only).
- Embedding **quality** on this corpus (no MTEB, no retrieval A/B).
- Two-pass extraction edge yield.
- Any fine-tune: no dataset has been exported and trained on yet.
