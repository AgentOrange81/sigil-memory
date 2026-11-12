# Retrieval benchmarks

What quipu's retrieval actually returns, measured with the shipped code, published with the
failures next to the score.

## The one rule

A number that is not in results/ does not exist.

Every number quoted below comes from a file in `results/`, and that file records the corpus
digest, the query-set digest, the git revision and the date it was produced. If a figure is not
in `results/`, it did not happen. That rule cuts both ways: it is not allowed to quote a number
that is not in a result file, and it is not allowed to leave a result file out of the record.

## What is measured

The shipped retrieval path, end to end. The harness re-implements nothing:

    quipu.store.write_entry   -> markdown notes
    quipu.index.reindex       -> SQLite FTS5 (unicode61 + porter)
    quipu.index.search        -> bm25 ranking, the same call the CLI makes

The corpus is written into a temporary vault, indexed by the real index, and queried with the real
`search`. A number is therefore the number an agent would get from `quipu search`, not a
re-implementation's idea of it.

## Datasets

### `synthetic` (default; no network, no download)

200 generated notes in 25 clusters of eight. A cluster is one subject area: eight notes about the
same subject, in the same domain, under one project, all mentioning the same rare marker term
(`kestrel`, `anvil`, `basalt`, ...). The clusters exist so that the corpus is not a list of
unrelated sentences: a query has to pick between eight similar notes, and a query built from the
subject alone has to compete with dozens.

The corpus is a pure function of `benchmarks/loaders.py`: same file in, same corpus out, every
run. The result file carries `corpus_sha256` and `query_set_sha256` so a run can be tied to the
exact corpus and questions it was scored on.

Four query kinds, one per cluster, 100 queries in total. Ground truth is defined by construction:
each query is generated from one chosen note, and that note's id is the gold.

| kind | what the query looks like | what it measures |
|---|---|---|
| `rare` | the marker term + the domain (`kestrel checkout service`) | can the retriever pick the right note out of eight near-identical ones? |
| `phrase` | the subject + a two-word phrase from the note (`postgres write path`) | does a phrase query find the note it came from, and does it rank first? |
| `paraphrase` | the same fact in different words (`purchase pipeline postgresql`) | can the retriever find a note that never used those words? |
| `question` | a natural question (`why did we choose postgres for atlas`) | what happens when the query is a sentence? |

The gold note rotates through the eight notes of each cluster, so no query kind is aligned with
the length of its gold note. BM25's bias toward short documents therefore hits every kind
equally, instead of being engineered into one class.

### `longmemeval` (local file only)

`--dataset longmemeval --path FILE` loads a local LongMemEval JSON file: each haystack session
becomes a note, and the notes containing the answer string are the gold. The dataset is never
downloaded. If the file is not there, the run prints `dataset not present, skipping` and exits 0
without writing a result file. There is no number for a dataset that was not run, and no
placeholder for one either.

## Metrics

- **R@k** - mean over queries of `|top-k hits ∩ gold| / |gold|`. With one gold note per query
  this is the hit rate.
- **MRR** - mean over queries of `1 / rank of the first gold hit` in the retrieved list. The list
  is capped at the largest requested k; a gold note outside it scores 0.

Both are reported per query kind, because one number over four different kinds of query says
nothing about which part of retrieval is weak.

## Results

From `results/synthetic-weighted-thin-2026-10-28.json` (corpus `synthetic v1`, 200 notes, 100
queries, git `af2a53f`, run 2026-10-29):

| | R@5 | R@10 | MRR |
|---|---|---|---|
| **all queries** | **0.64** | **0.90** | **0.3182** |
| rare | 0.56 | 1.00 | 0.2779 |
| phrase | 1.00 | 1.00 | 0.4300 |
| paraphrase | 0.48 | 0.60 | 0.2680 |
| question | 0.52 | 1.00 | 0.2971 |

Ten of the 100 queries still miss, and **all ten are `paraphrase`**: the note that answers the
question never uses the asker's words. That is the vocabulary gap an embedding or hybrid ranker
exists to close (PLAN.md M2), not something a better lexical weighting reaches.

From `results/synthetic-bm25-2026-10-28.json` (git `77b5dd3`, run 2026-10-28) — the same harness
before #25, #31 and #43 landed, kept because a score nobody can compare is a score nobody can
audit:

| | R@5 | R@10 | MRR |
|---|---|---|---|
| **all queries** | **0.51** | **0.62** | **0.2422** |
| rare | 0.64 | 1.00 | 0.2987 |
| phrase | 1.00 | 1.00 | 0.4267 |
| paraphrase | 0.40 | 0.48 | 0.2433 |
| question | 0.00 | 0.00 | 0.0000 |

### What the misses had in common (the 2026-10-28 run)

38 of the 100 queries did not return their answer within the top 10 in the first run of this
harness. They failed in three distinct ways, all properties of the retrieval stack rather than of
the corpus, and two of the three are now fixed — the list is kept because the fix is only credible
next to the failure it removed:

1. **One absent word kills the whole query (25 queries, the `question` kind).** `_fts_query`
   joins every token with `AND`, and the last token is a prefix match. A question contains an
   interrogative ("why", "did", "choose") that appears in no note, so the query matches nothing
   at all - zero results, not bad results. This is the single biggest lever in the table, and it
   is why `docs/USING.md` tells an agent to search with a topic phrase (`quipu search "DOCP"`)
   rather than with the question. The matcher does not stopwords, does not OR, and does not fall
   back to a partial match when a token is unknown. **Fixed** (#25, #43): an empty AND result
   falls back to the OR ranking, and a *thin* one is topped up from it — question R@5 0.00 -> 0.52,
   R@10 0.00 -> 1.00.
2. **The vocabulary is the note's, not the asker's (13 queries, the `paraphrase` kind).** The
   query returns three notes that carry both synonyms - the right area, the wrong note - and the
   gold note is missing because that note never used the synonym the asker used. All 13 of those
   misses never retrieve the gold at any rank up to 10. This is the failure an embedding or
   hybrid ranker would fix, and it is exactly the gap PLAN.md M2 names. **Not fixed**: these are
   the only ten misses left.
3. **Near-duplicate notes win on length (9 of the `rare` queries, all recovered by k=10).** Every
   `rare` gold is inside the top 10; 36% of them fall out of the top 5 because seven shorter
   notes in the same cluster mention the same terms more densely. BM25's length normalisation
   prefers them. The `phrase` kind shows the same effect at rank 1: R@5 is 1.00 but MRR is only
   0.43, so the right note is usually found and usually not first. **Partly fixed** (#31): the
   column weights moved the answer above a body-only match, and lifted live-vault rank-1 precision
   from 5/10 to 9/10, but on this corpus phrase MRR is unchanged at 0.43 — the length bias inside
   one column is still BM25's.

The two kinds that work are the two where the query's words are literally in the note. The
retriever currently reads the corpus; it does not yet understand the question.

## The live vault, which is not a synthetic corpus

`benchmarks/live_queries.py` runs ten hand-judged questions against a COPY of a real vault and
prints the ranking for each. It exists because the synthetic corpus is too kind: every cluster shares a
rare marker term with its gold note, so it cannot reproduce the failure that started this — an
870-note vault where an agent asks a question and gets a pasted transcript as the answer.

    cp -a ~/quipu-vault /tmp/livevault
    .venv/bin/python -m benchmarks.live_queries /tmp/livevault --json out.json

It reports four columns, and only one of them is an accuracy (#47):

- `rank1_gold` — rank 1 is one of the notes **hand-verified as an answer** to that question. Quote this.
- `rank1_promoted` — rank 1 survived the promotion gate rather than arriving `raw`/`pruned`.
- `rank1_subject` — rank 1 merely mentions the subject (one required stem per question). Loose on
  purpose, and **not** precision: a hardware audit that says the word "gateway" scored for "which model
  is the gateway default". Trend only.
- `gold_in_5` — the verified answer is in the top five, even when rank 1 is wrong.

Measured 2026-10-29 on a copy of the 870-note live vault (`benchmarks/results/live-2026-10-29.json`).
The first row calls the shipped `quipu.index.search`; the rest are the harness's own copy of the ranking,
run to A/B a shape:

| variant | rank 1 gold | rank 1 promoted | rank 1 subject | gold in 5 |
|---|---|---|---|---|
| **shipped, after writing the two missing entries** | **8/10** | 8/10 | 10/10 | 9/10 |
| shipped, before that curation | 6/10 | 8/10 | 9/10 | 7/10 |
| copy: AND only, flat columns | 7/10 | 7/10 | 8/10 | 7/10 |
| copy: thin AND, flat columns | 7/10 | 6/10 | 9/10 | 8/10 |
| copy: thin AND, shipped column weights | 8/10 | 8/10 | 10/10 | 9/10 |
| copy: flat + raw×0.5, pruned×0.1 | 8/10 | 9/10 | 10/10 | 8/10 |
| copy: shipped + raw×0.5, pruned×0.1 | 8/10 | 9/10 | 10/10 | 9/10 |
| copy: summary-only (10/1/1/0.2) | 8/10 | 9/10 | 10/10 | 9/10 |

Rows 3–8 run against the curated vault (`live-2026-10-29-curated.json`); the two shipped rows are the
same instrument before and after the curation, which is the cleanest measurement in this file: **+2
verified answers from two entries, with no code change at all**. The variant rows are measured after
curation because that is the corpus they will be judged on.

Three things a reader should take from that table.

**The first honest reading was 6/10, not 9/10.** The earlier table in this file quoted 9/10, and it was
measured with the subject column. Under `rank1_gold`, four questions were wrong at rank 1: a 3900X memory
note answered a 3960X question, a solana-test-validator capture answered "which GPU should I buy", a
launcher-UI note answered "how do we deploy a pump.fun token", and a migration plan with no addresses in
it answered "rig IP addresses". Two of the four had **no entry in the corpus that answers them at all**
(`gold_ids` was empty and the row printed `NO VERIFIED ANSWER IN CORPUS`) — so two verified entries were
written, and the same ranking then scored **8/10** on the same ten questions. Curation, not weights.

**The copy is faithful, or the row means nothing.** The "copy: thin AND, shipped column weights" row
reproduces the shipped row exactly (8 gold / 8 promoted / 10 subject / 9 in-five), which is the only reason
to believe the rows around it. A variant that disagrees at the baseline is measuring its own
implementation — this file used to carry a row labelled "shipped weights" that was `10/6/3/1` while the
code shipped `10/4/2/0.5`. The weights are now read from `quipu.index.COLUMN_WEIGHTS` and pinned by a test.

**Sharpening the weights does not buy answers.** The summary-only row lifts the loose column to 10/10 and
buys exactly zero verified answers over the shipped ranking (8/10 either way). State demotion lifts
promoted-at-rank-1 to 9/10 and also buys zero. Both are the same trap: the metric that moves is the one
that is not precision. The shipped combination stays chosen on the harness tie-break, not on the live set —
ten hand-judged questions overfit, and a sharper weight set once scored 10/10 here while losing 0.002 MRR on
100 queries. The two misses that remain are `gold_in_5` hits: right note in the list, wrong note first, and
both need a curated entry or a better ranking, not a multiplier.

`benchmarks/sweep_retrieval.py` is the other instrument: it installs each variant over the shipped
`search` and prints every one, so a weighting that fails the measurement gets reverted in public
rather than quietly kept.

### Rejected: duplicate consolidation (#55)

Consolidation — folding near-duplicate entries into one evidence-backed belief — was measured before
building. Two independent signals, same vault (294 promoted entries):

Exact duplicate summaries: **0**.

Token-Jaccard clustering, same project:

| threshold | clusters | entries covered |
|---|---|---|
| 0.55 | 3 | 7 |
| 0.65 | 1 | 2 |
| 0.75 | 1 | 2 |
| 0.85 | 0 | 0 |

Semantic sweep over the stored embeddings (cosine ≥ 0.86): **206 pairs**, 82 of them with differing
numbers on both sides (contradiction candidates) and 124 restatements — but the prefixes dominating the
pair count give the game away:

```
 380  "'corrected metadata for"     <- one family, 20 entries, one TRUNCATED IDENTICAL PREFIX
   3  "'fabric curation phases"
   2  "'launchpad v2 decision:"
```

190 of the 206 pairs come from that single boilerplate family, which was an importer defect (#54), not
a duplication of belief. A consolidator would have spent its first pass merging truncated prefixes and
called it progress.

**Verdict: not built.** Three clusters at the loosest useful threshold is 2% of the corpus, and the
similarity signal on this vault is dominated by damage that needed deleting, not merging. Reproduce with
`benchmarks/consolidation_probe.py <vault>`; the embeddings must exist (`quipu embed`, see the section
above).

What the corpus needs instead is the opposite of merging: 17 of 863 entries carried an `ask` when this
was measured — the field that makes an entry trainable (#27) — and 20 promoted entries carried no claim
at all (#54).

### Semantic retrieval, fused with bm25 (#5)

`results/synthetic-hybrid-2026-10-29.json`, produced by `benchmarks/sweep_hybrid.py` against a local
`llama-server --embeddings` running `nomic-embed-text-v1.5.f16` on CPU:

| retriever | R@5 | R@10 | MRR | para / phrase / question / rare R@5 |
|---|---|---|---|---|
| bm25 (shipped) | 0.640 | 0.900 | 0.318 | 0.48 / 1.00 / 0.52 / 0.56 |
| vector only | 0.770 | 0.920 | 0.393 | 0.56 / 0.96 / 0.80 / 0.76 |
| rrf, vector 0.5 | 0.700 | 0.920 | 0.338 | 0.56 / 1.00 / 0.64 / 0.60 |
| rrf, vector 0.65 | 0.750 | 0.920 | 0.366 | 0.56 / 1.00 / 0.76 / 0.68 |
| **rrf, vector 0.8 (shipped)** | **0.760** | **0.910** | **0.383** | **0.56 / 1.00 / 0.76 / 0.72** |
| rrf, vector 0.9 | 0.770 | 0.910 | 0.391 | 0.56 / 0.96 / 0.80 / 0.76 |

Pure vector retrieval has the best headline number and gives up exact-phrase recall (1.00 -> 0.96) —
the one query shape where lexical matching is simply right. 0.8 keeps phrase at 1.00 for 0.01 R@5,
which is the trade the shipped fusion takes. `--hybrid` is opt-in for that reason, and on the curated
live vault (ten hand-judged questions) it scored 8/10 against bm25's 10/10: semantic neighbours do not
know which claim was superseded.

An earlier attempt with a chat model as the embedder (Qwen3-4B, mean pooling) scored vector-only 2/10
on the live questions — a decoder model's pooled states are not an embedding space. Use a real
embedding model.

### Rejected: query shaping and focus scoring (#48)

A blob — a session report, or a multi-topic "pack" entry — can outrank a focused note for any single
topic inside it: bm25 rewards coverage, not concentration, and the AND includes function words
("which", "is", "the", "for", "this"), so a 4,000-word report matches everything a focused note fails
to match. `benchmarks/sweep_focus.py` measures the two candidate fixes separately, riding on the
shipped `search` (the stopword variant rewrites the query text, the focus variant re-scores the
shipped candidate window — neither reimplements retrieval).

Measured 2026-10-29, synthetic (100 queries) and live (ten hand-judged questions):

| variant | live ok | live promoted | R@5 | R@10 | MRR |
|---|---|---|---|---|---|
| shipped | 9/10 | 7/10 | 0.640 | 0.900 | 0.318 |
| strip stopwords | 9/10 | 6/10 | 0.650 | 0.900 | 0.319 |
| focus factor | 9/10 | 6/10 | 0.650 | 0.900 | 0.319 |
| both | 9/10 | 6/10 | 0.660 | 0.900 | 0.320 |

**Rejected.** +0.02 R@5 for a maintenance-bearing stoplist plus a second scoring term, no live gain,
and a worse one — promoted-at-rank-1 drops 7 -> 6. The live failures were not blobs at all; they were
facts the vault did not contain, and writing those entries fixed the answers with no code change
(rank-1 promoted 6/10 -> 8/10 across three curated entries). The negative result is recorded here so
the next person does not re-run the same three variants hoping for a different answer.

## Honesty rules

- A number that is not in `results/` does not exist. No number appears in this file that is not
  in a result file.
- The corpus is synthetic and its difficulty is by construction. It measures the retrieval
  stack on a corpus with known ground truth; it is not a claim about anyone's real vault, and it
  is not comparable to a LongMemEval or LoCoMo score.
- Failures are published, not summarised away. Every result file lists the queries that missed,
  with the notes it returned instead.
- A dataset that was not run has no number. `--dataset longmemeval` without a local file prints
  `dataset not present, skipping` and exits 0; it does not guess.
- The corpus and the query set are deterministic and digested, so a rerun must reproduce the
  same numbers. If it does not, the harness is broken and the result file is wrong.
- BM25 only. No embeddings, no hybrid scoring, no graph expansion: PLAN.md M2 has not landed
  those yet, and this harness measures what exists today.

## Reproducing

    cd /home/dirk/Projects/quipu-work/bench-harness
    .venv/bin/python -m benchmarks.run --dataset synthetic --k 5 --k 10 \
        --json benchmarks/results/synthetic-bm25-$(date +%F).json
    .venv/bin/python -m pytest -q                 # the shipped suite stays green
    .venv/bin/python -m pytest benchmarks/tests -q   # the harness's own tests
    .venv/bin/python -m ruff check .

    # the variants behind the shipped weights, and the live-vault check
    .venv/bin/python -m benchmarks.sweep_retrieval
    cp -a ~/quipu-vault /tmp/livevault
    .venv/bin/python -m benchmarks.live_queries /tmp/livevault

The harness's own tests live in `benchmarks/tests/` so the shipped suite (`tests/`) stays
untouched. They pin the three things a number depends on: the corpus is deterministic, the
metrics are what they claim, and a missing dataset produces no number.

LongMemEval, when a local copy exists:

    .venv/bin/python -m benchmarks.run --dataset longmemeval --path /path/to/longmemeval.json \
        --k 5 --k 10 --json benchmarks/results/longmemeval-bm25-$(date +%F).json

## What this harness does not tell you

- Whether quipu retrieves well on a real vault. The corpus here is generated, with near-
  duplicates on purpose, so the absolute numbers are pessimistic by design; only the ranking of
  the query kinds and the shape of the failures are meant to carry over.
- Whether a hybrid or embedding ranker would do better. That comparison needs the same harness
  with a second retriever plugged in, which is the next step of M2.
- Anything about LoCoMo, which is not wired up yet.
