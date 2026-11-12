# Using sigil

Day-to-day notes for a working vault. Every command here exists on `main` today; the output is
real, quoted from runs against a throwaway vault.

## The habit

Recall before you answer. If a question touches project history — what was decided, why, what was
tried and failed — search the vault *before* answering from your own context:

```bash
sigil search "DOCP" --project wraith --limit 3
```

```
1. [0af4c584] raw      capture    the 3600 DOCP config threw 649 CCEs in one boot
   the 3600 <<DOCP>> config threw 649 CCEs in one boot
2. [525f74c3] promoted decision   DOCP 3600 is stable again after the BIOS 2601 flash
   <<DOCP>> 3600 is stable again after the BIOS 2601 flash
```

BM25 over the entries, filterable with `--kind`, `--state`, `--agent`, `--project`. No match prints
`no matches` and exits 1. For a question about a specific thing, ask the graph too —
`sigil timeline trx40` shows every edge with its validity window, so you see what changed and when:

```
   1 2026-10-28T22:06:49 → 2026-10-28T22:07:06  trx40 -measured_as-> docp-3200
   2 2026-10-28T22:06:50 → 2026-10-28T22:07:06  trx40 -measured_as-> docp-3600
   3 2026-10-28T22:07:06 → now  trx40 -measured_as-> docp-3200
```

Search finds what was written; the timeline shows what changed.

## Three ways in

| command | what it is | when to use it |
|---|---|---|
| `sigil capture` | a raw capture: verbatim, unfiltered, `state=raw` | anything you might need later and have not judged yet — cheap, and it may never be promoted |
| `sigil write` | a curated entry you already trust, written directly | you know the fact and the evidence now, and want no model in the loop |
| `sigil reflect <transcript>` | a model reads a transcript and *proposes* entries; the gate decides | at the end of a session, when the outcome is in a transcript and you want it distilled rather than dumped |

`capture` and `write` take the text as an argument or on stdin. `reflect` runs one pass, promotes
the proposals that clear the gate, and leaves the rest raw.

A fourth door is `sigil import fabric <dir>`, which reads an existing Icarus/fabric corpus into the
vault so an old archive becomes recallable through the same `search` / `timeline` / `export` paths.
`--dry-run` reports what it would do and writes nothing — `{"dry_run": true, "written": 1,
"skipped": 1, "by_state": {"promoted": 1}}`. `training_value: low` entries arrive as `state=pruned`
with a `pruned:low-training-value` tag, never as promoted; `--include-noise` imports them instead
of skipping them.

## What the import reads, and what it leaves behind

The importer reads **top-level `*.md` only**, on purpose: a corpus keeps its stubs and duplicates
in sibling directories, and those are not what an import is for. The report makes that policy
visible instead of leaving it implicit — `directories_ignored` says what the policy left behind,
counting markdown files in each ignored subdirectory at any depth, and `skipped_by_reason` says
why each file was skipped: `ignored name`, `empty body`, `no type`, `unparsable frontmatter`,
`noise (training_value: low)`.

The shape of a report, quoting a real run over the archived corpus:

```json
{"written": 846, "skipped": 19,
 "skipped_by_reason": {"ignored name": 1, "noise (training_value: low)": 16,
                       "unparsable frontmatter": 2},
 "directories_ignored": {"_dupes": 481, "_meta": 1, "_stubs": 4080, "daily": 66, "templates": 7},
 "by_kind": {"decision": 556, "conclusion": 177, "capture": 96, "handoff": 17},
 "by_state": {"promoted": 277, "raw": 512, "pruned": 57}}
```

Since #26 an entry that claims `training_value: high` is promoted only if it also carries
evidence — the rest arrive `raw` and tagged `import:no-evidence`. A label inherited from another
system is a claim, and the import applies the same gate `promote.py` applies before agreeing
with it.

A corpus that keeps its entries deeper than one level can be imported with `--recurse`, which
refuses `_dupes`, `_stubs`, `_meta`, `templates`, `.obsidian` and `assets` by default; `--skip-dir`
replaces that list and can be repeated. A duplicate id is not a skip: the second file sharing one
keeps a stable id derived from its path, because dropping a file a neighbour already named loses
data for the sake of a tidier report.

## What makes an entry worth promoting

The gate is one line, in `sigil/promote.py`:

```python
promoted = p.importance >= cfg.promote_threshold and bool(p.evidence)
```

`promote_threshold` is `0.5` in `.sigil/config.toml`. Both halves must hold.

**The summary is a statement, not a topic. The evidence is something a skeptic could re-check** — a
number, a command, a path, a log line. **One fact per entry. The `ask` is self-contained**,
because it is the prompt half of a training example. The CLI's own reflection prompt
(`prompts/promote.md`) says all four, with these examples:

```
"summary": "TRX40 needs DOCP 3200 for stability because 3600 produced 649 CCE events in one boot"   <- good
"summary": "RAM tuning"                                                                            <- bad
"ask": "why did the box reboot at 03:02?"                                                          <- good
"ask": "did that fix it"                                                                           <- bad
```

A topic cannot be checked; a statement can. No evidence means the entry is not promoted. On export
an `ask` shorter than 20 characters or under three words is dropped — a conversation turn, not a
prompt.

**The gate asks whether evidence exists; `evidence_kind` records what it is.** Every entry written from
a proposal gets one, classified from its own evidence text — no model call, no drift:

| kind | means | example |
|---|---|---|
| `command` | something a person could re-run | `` `sigil reindex` dropped it `` |
| `path` | something a person could open | `sigil/hook.py:76` |
| `url` | something a person could fetch | `https://…/runs/42715230290` |
| `claim` | prose that points at nothing re-checkable | `see log` |

`claim` is not a rejection: a 9-byte "see log" is a weak memory, not a false one, and refusing it would
lose the claim while still leaving the corpus unable to say how much of it is re-checkable. It is
recorded, indexed, and filterable: `sigil stats` reports the mix and the evidence-size distribution, and
`sigil export sft` takes `--evidence-kind command,path,url` and `--min-evidence-bytes 60` to hold the
training set to the strong half without touching the vault — filtering is a view, not a delete.

## Correcting something

When you learn an entry was wrong, do not edit it and do not delete it. Write the new version and
point it at the old one:

```bash
sigil write --kind decision --state promoted \
  --summary "DOCP 3600 is stable again after the BIOS 2601 flash" \
  --evidence "3 consecutive boots, 0 MCE lines each" \
  --ask "why did the box reboot at 03:02?" --project wraith --agent glitch \
  --importance 0.95 --supersedes e2314ae5 --edge "TRX40:measured_as:DOCP 3600"
```

`--supersedes` takes an entry id and is repeatable. `--edge` is `SRC:PREDICATE:DST` and is
repeatable too. This is what produces a preference pair later: `sigil export dpo` walks the
`supersedes` links and emits one pair per link — the new entry as `chosen`, the old one as
`rejected`, under the prompt the pair shares (meta abbreviated; `session` and `confidence`
elided):

```json
{"prompt": "why did the box reboot at 03:02?",
 "chosen": "DOCP 3600 is stable again after the BIOS 2601 flash\n\nEvidence: 3 consecutive boots, 0 MCE lines each\n\n## Supersedes\n\n- [[e2314ae5]]",
 "rejected": "DOCP 3200 with FCLK 1600 is stable on the TRX40; 3600 was dropped after 649 CCEs\n\nEvidence: 649 MC25_STATUS CECC events in boot -1; 0 MC25 lines in the 3 boots since",
 "meta": {"sigil": {"entry": "525f74c3", "kind": "decision", "state": "promoted"},
          "superseded": {"entry": "e2314ae5", "kind": "decision", "state": "promoted"},
          "reason": "3 consecutive boots, 0 MCE lines each"}}
```

Both claims, and the evidence that decided it in `meta.reason`. That is the signal the project
exists to produce: the memory changed its mind, so it already knows which answer was better.

**The graph follows `--supersedes`.** Writing the correction closes the live edges the superseded
entry asserted, dated at the instant the correction was written, and the closure is recorded in the
markdown — so `sigil timeline` shows one live window (`→ now`) for one fact, and
`rm .sigil/index.db` + `sigil reindex` reproduces the same history instead of resurrecting the old
belief. An edge the correction restates itself is left open: restating a fact under a new entry
corrects the carrier, not the belief.

Two things stay in your hands. `sigil invalidate <edge-id>` closes a single window you simply want
gone, and `sigil contradict <edge-id> src pred dst` closes it and asserts a replacement in one
transaction. Edges are never deleted; the closed row keeps its window and `sigil export graph` marks
it `"live": false`. `sigil audit` reports any superseded entry whose edge is still live, which is
the shape a vault written before the rename is in.

### Project hubs: a view over the tag, not an edge per entry

A real vault is mostly tag and very little graph — 891 entries and 3 edges is what the live one looked
like, and 466 of those entries carried a project tag. The tempting fill is one `part_of` edge per entry,
which means writing 466 edge specs into 466 markdown files, and `graph.rebuild` folds db edges back into
the markdown on every reindex, so it would rewrite them forever. The tag is already the assertion and it
is already in the markdown, so the hub is derived at read time instead:

```bash
sigil neighbors shitter      # entries tagged `shitter`, after the edges
sigil timeline glitch        # the same view, dated by each entry's own creation
```

Nothing is written; `stats` reports `entries_with_an_edge` and `edges_by_predicate` so the gap is
visible rather than assumed. Two rules keep the view honest: `unknown` is never a hub (it is a bucket,
not a claim — 425 entries carry it), and `pruned` entries are left out unless you pass `--all`, because
a browse surface that leads with rejected material teaches an agent to cite it. On the live vault that
one rule took `neighbors shitter` from transcript-summary junk to 69 promoted entries first.

### Citations: what points at this

An entry that means something to another entry names it — `[[e2314ae5]]`, a supersedes link, the id in a
note about a decision. That is a citation and it is derived the same way, because the body that makes it
stays true forever:

```bash
sigil neighbors e2314ae5     # what this entry cites, and what cites it
```

An 8-hex word is only a citation if the corpus has that entry, so a short commit sha is not one. Nothing
is written; `sigil audit` reports an edge whose predicate is outside the closed vocabulary (defaults plus
whatever `.sigil/config.toml` adds), live or closed, because a graph rots into 400 spellings of "uses"
quietly.

A hub is a place to look, not a claim about how things relate. For those, assert a real predicate —
`--edge` on the CLI, `edges` on `sigil_write` through the provider — and keep the vocabulary closed:
`causes`, `depends_on`, `implements`, `supersedes`, `decided_against`, `measured_as`, `located_in`,
`used_by`, `blocks`, `owned_by`, `part_of`, `contradicts`, `learned_from`.

## Repairing the summaries an older import mangled

An import from before the frontmatter fix left 88 notes with a quoted, truncated summary. The repair reads
each one's origin and rewrites the note in place:

```bash
sigil --vault ~/vault repair
```

It is a dry run until you pass the apply flag, and it prints what each note would become, prefixed by
where the fix came from: `fabric:<file>` when the fabric original was found by its frontmatter id,
`body` when the note's own body carries the continuation the wrap cut off, or `quotes-only` when the text
was complete and only the quotes were wrong.

It never guesses. A summary that ends mid-word with no origin and no continuation in the body is reported
and left exactly as it is — four notes on this box are in that state, and editing them is a human or
model judgement call, not a repair. Nothing is renamed: the file keeps its path, so no second note appears
under a new slug.

Measured on the live vault: 88 quoted summaries, 74 recovered from the fabric origin, 10 from the note's
own body, 10 more as quotes-only, and 4 left as findings. A second run finds nothing to do.

### An edge the index holds and the markdown does not

`sigil edge` records every edge into the entry that asserted it, because the markdown is the truth and
`rm .sigil/index.db` must lose nothing. A carrier that names no entry breaks that: the row lands in
SQLite, nothing records it, and the next rebuild silently drops a live belief. The command now refuses
such a carrier instead of ignoring it — `--note` takes an entry id, never a description — and a vault
written before that rule can be repaired:

```bash
sigil --vault ~/vault repair --edges            # dry run: edge id, spec, the entry that will carry it
sigil --vault ~/vault repair --edges --apply    # write the spec into that entry's frontmatter
```

The carrier is derived from the edge's own `src`, which is the entry that asserted it, and the row's
`note_id` is only a fallback — that field is exactly where a bad caller wrote prose. An edge with no
entry to carry it is reported with the candidates it tried and left alone; the command exits non-zero
while any remain, the same contract the summary repair makes. `sigil audit` reports them under
`edges_not_in_markdown` in the meantime.

## Importing fabric: quoted and wrapped summaries

Fabric's own writer emits frontmatter values as YAML **single-quoted** scalars and wraps long ones onto a
continuation line:

```yaml
summary: 'Corrected metadata for PRL mining fleet details (agent: hermes, project_id:
  prl-mining)'
```

The importer folds those back into one value and strips the quotes, so an import yields the same handle
fabric wrote. If you imported before that fix, `sigil audit` names every entry still carrying
a leading quote character — the parser fix does not rewrite notes that are already on disk, and the audit
is how the damage stays visible.

## Reflecting a live Hermes session

Hermes writes `profiles/<p>/sessions/<id>.jsonl` for exported sessions, **not** for the ones it is
running: on this box the newest file there is from 2026-10-28 while `state.db` holds sessions through
today. A hook pointed at that directory reflects the past and nothing else, and exits 0 doing it. Read
the live session from the db instead:

```bash
sigil --vault ~/vault hook --session-db ~/.hermes/profiles/glitch/state.db --session f71c6b37 \
  --agent glitch --project sigil
```

`--session` takes an id, or any fragment that matches exactly one — a 64-hex id is not something a
human types, and an ambiguous fragment is refused rather than guessed. The reader takes **prose**
(`user` and `assistant` turns) by default: a measured session held 376 KB of rows against 18 KB of
prose, and the decisions, measurements and corrections live in the prose. A pass over one such session
produced 4 promoted entries in 102 s on the 35B tier, one chunk, no chunking needed.

`reflect` takes the same flag. `--include-tools` (library: `include_tools=True`) adds
`tool <name>: …` lines capped at 2000 characters and marked `…[truncated]`, for the case where the
evidence is in a tool result. An assistant row with no content is a tool-call turn, not a silent turn:
it is skipped and counted, never rendered as an empty `assistant:`.

## Restricting a search to a time window

Entries have always carried `created`; search now filters on it, so "what did we decide in October" is
a query instead of a list you read dates off.

```bash
sigil --vault ~/vault search "glitch memory" --since 2026-10-01
sigil --vault ~/vault search "quipu" --until 2026-10-28
sigil --vault ~/vault search "release" --since 2026-10-01 --until 2026-10-29
sigil --vault ~/vault search "release" --since 2026-10-01T12:30:00Z
```

A bare date names a **day**, which is the only sane reading of the flag: `--since 2026-10-01` starts at
00:00:00Z on the 1st, and `--until 2026-10-29` runs *through* the 29th — including a note written at
23:59. Give an explicit instant (`2026-10-29T00:00:00Z`) and `--until` becomes exclusive, exactly as
written. Offsets are normalised to UTC, and an unparseable value is a usage error that names what it got,
never a filter that quietly disappeared:

```
$ sigil search quipu --since october
--since/--until: 'october' is not a date or an ISO instant — use 2026-10-01 or 2026-10-01T12:30:00Z
```

The window joins `kind`/`state`/`agent`/`project` in the same `WHERE`, so ranking is untouched and
`--limit` counts survivors. Measured on the live vault: 40 hits for `quipu` with no window (18 on
2026-10-28, 22 on 2026-10-29); `--until 2026-10-28` returns exactly those 18 and `--since 2026-10-29`
exactly those 22 — a clean partition, no day dropped either side.

## The brief: what an agent reads before it knows what to ask

A vault answers a question well. It does not help an agent that has just woken up and has no question
yet — so `sigil brief` writes the standing answer: promoted entries only, one line each, grouped by kind,
highest importance first.

```bash
sigil --vault ~/vault brief                      # -> .sigil/brief/vault.md, 40 entries
sigil --vault ~/vault brief --project sigil      # -> .sigil/brief/sigil.md
sigil --vault ~/vault brief --stdout --limit 0   # everything, to a pipe
```

It is deterministic and model-free: no retrieval, no ranking, no LLM call, so the same vault always
renders the same bytes. It is also **derived**, like the FTS table: it lives under `.sigil/brief/`, never
beside the entries in `notes/`, because a generated page among the entries would be ingested as an entry
and rank for everything it mentions.

The default is 40 entries (about 10 KB / 2.5k tokens). Measured on the live vault, all 289 promoted
entries render 81 KB / ~20k tokens, which is a document rather than a boot page; `--limit 0` is there for
a human, not for a context window.

## One sentence per summary, and the limit enforces it

`sigil write` and the MCP `write` tool refuse a `summary` over **280 bytes**:

```
summary is 412 bytes, limit 280: this is a blob, not a handle. Put the detail in --body and keep one
sentence here (the first 280 bytes are: 'Resolved the issue where the ComfyUI server...'…)
```

The summary is the retrieval handle — it is what bm25 ranks, what a brief line shows, and what an SFT
example is built from. Blobs are the entry class that ranked badly for three separately measured reasons
in October, and no ranking lever fixed them, because the input was wrong. So the writing is where they
stop. `--allow-long-summary` exists for a deliberate exception; importers are unaffected, since the limit
sits on the capture surfaces and not in `write_entry` — migrating 800 historical notes must never be
blocked by a rule about new ones.

Size is not the only way a handle stops being a handle. A summary that opens with `{`, `[` or a code
fence is a serialised object or a pasted block, and its tokens rank exactly as well as prose's — which
is how twenty-one entries on a real vault came to be competing for a 400-token recall budget with text
no agent could read. Both capture surfaces now refuse the shape:

```
summary starts with '{"corrections_verified": [ {"correction": "(a) start() adds NO listeners"}]}', which
is a serialised blob, not a statement: the summary is the retrieval handle and its tokens rank, so a JSON
body or a fenced block crowds real recall. Write one sentence here and put the blob in --body (or pass
--allow-summary-blob if this really is the handle).
```

`sigil capture` is friendlier, because its body *is* the capture: it takes the first prose line as the
handle and keeps the blob in the body. The importer does the same for a historical note and refuses only
when the body holds no prose at all, reporting `summary not prose` in `skipped_by_reason` — losing a
memory to tidy a field is the worse trade. `sigil audit` reports `blob summary` for anything that got in
before the rule existed.

## Optional semantic search

`sigil search` is lexical (SQLite FTS5 + bm25) and works with nothing else installed. When a vault's
entries use different words from the questions you ask, you can add embeddings from any local
OpenAI-compatible endpoint — sigil itself still has zero dependencies, it just POSTs text:

Start any OpenAI-compatible `/v1/embeddings` service on the box first — llama.cpp's server in
embedding mode with mean pooling (a `nomic-embed-text-v1.5` f16 GGUF on CPU is enough: 858 entries
embedded in 37 s), ollama, or anything else speaking that shape. Then:

```bash
sigil --vault ~/vault embed --url http://127.0.0.1:8083/v1/embeddings --model nomic-embed
sigil --vault ~/vault search "did we already try the other approach" --hybrid \
  --embed-url http://127.0.0.1:8083/v1/embeddings --embed-model nomic-embed
```

Or put the endpoint in `.sigil/config.toml` and drop the flags:

```toml
[embed]
url = "http://127.0.0.1:8083/v1/embeddings"
model = "nomic-embed"
```

`embed` is idempotent — it re-embeds only entries whose text changed, so it is cheap to re-run after a
`reindex`. Vectors live in `.sigil/index.db` and are derived data: losing the index costs one re-embed,
never a memory, and `--hybrid` with no vectors or no endpoint falls back to the lexical ranking and says
why on stderr.

**When it helps, measured** on the 100-query synthetic harness (200 notes, nomic-embed-text-v1.5 on
CPU): R@5 0.640 -> 0.760, MRR 0.318 -> 0.383, and the gain lands exactly where lexical search is blind
— `question`-shaped queries 0.52 -> 0.76, `rare` 0.56 -> 0.72, `paraphrase` 0.48 -> 0.56 — while exact
phrases stay at 1.00 because the fusion keeps the lexical ranking at 20% of the weight.

**When it does not.** On a curated vault whose entries already carry the asker's words, bm25 is
saturated and semantic neighbours can surface a note that is topically right and factually stale — on
the live 850-entry vault the ten hand-judged questions scored 8/10 under hybrid against 10/10 under
bm25. Semantic similarity has no notion of "that was superseded"; the graph does. Use `--hybrid` for
questions that share no vocabulary with the corpus, not as a default.

## The question an entry answers

An entry carries two halves: the `summary` (the claim) and the `ask` (the question a searcher would have
asked to find it). `sigil export sft` needs both — an entry without an ask is recall only, it teaches
nothing — and a vault migrated from an older system mostly has no asks, because the old format never
stored one. `ask-backfill` writes them:

```bash
sigil --vault ~/vault ask-backfill --count         # how many entries have no ask
sigil --vault ~/vault ask-backfill --sample 5      # print five proposals, write nothing
sigil --vault ~/vault ask-backfill --apply         # write the ask lines in place
```

One model call per entry, temperature 0, no chain, no tools, against the OpenAI-compatible endpoint in
`.sigil/config.toml` (`[llm] base_url`; `--url`/`--model`/`--api-key` override it, and a cloud key is the
usual reason to). Validation is the guard rail, not the model: an answer must be one line ending in `?`,
between 20 and 200 bytes, and not a restatement of the summary with a question mark bolted on. Anything
else — prose, a quote, `NONE`, a question cut off mid-word — costs that entry and nothing else, and is
reported as `skipped` with the reason.

The `max_tokens` cap is 400 on purpose: a reasoning model spends the budget on `reasoning_content`
first, and at 80 it truncated most answers, which then read as a bad model instead of a bad cap.

Writing is in place: the `ask:` line lands after `summary:`, the path does not change, and the body is
not touched — the same discipline `sigil repair` follows, so no second note appears under a new slug.

Measured on the live vault (870 entries, 301 promoted): `--count` reported 265 missing, a corpus run
wrote 241 and rejected 36, and `sigil export sft` went from 18 examples to 265.

## Hooking a session end

A framework can call one command when a session ends, without embedding sigil and without deciding
anything about memory itself:

```bash
sigil --vault ~/vault hook --transcript session.md --agent glitch --project sigil
```

It reads the transcript (a path, or `-` for stdin), runs the reflection pass, writes what survives the
promotion gate, and indexes it. The contract is the exit code, because that is all a framework reads:
**0 only if the pass completed**, non-zero with the reason on stderr otherwise. A transport error, an
unparseable response, a missing transcript or a hung endpoint is a failure.

A long transcript is chunked, and **each chunk is written as it finishes**, so a failure on chunk 20 of
24 keeps the nineteen that landed and says so:

```
reflection failed at chunk 20 of 24: JSONDecodeError: … (19 chunk(s) applied, 64 entries written)
```

The chunks already applied are recorded in `<vault>/.sigil/chunks/<session>.json`, keyed by the chunk's
text, so a retry reads only what did not land — and that record is derived state, like the index:
delete it and the next pass simply re-reads everything. A claim an earlier chunk already wrote is never
written twice by a later one.

- `--timeout` (default 300 s) bounds **the whole pass**, not one request: three chunks that each take
  100 s are not a 300 s pass when a framework is waiting on it. Scale it with the chunk count —
  `--max-chars` decides how many there are — or a long session fails at the deadline however fast the
  model is. Measured: a 348 KB session is 24 chunks at `--max-chars 12000` and needs ~900 s.
- `SIGIL_HOOK_DRY_RUN=1` (or `--dry-run`) prints the exact prompt and writes nothing — the way to check
  the hook without spending tokens.
- A pass that completes but proposes nothing exits 0 and says so on stderr. That is a legitimate
  outcome for a thin transcript, and it must stay visible: a hook that runs for a week and writes
  nothing looks exactly like this.

Measured: a 3 KB transcript against the local `qwen3.6-35b-a3b` took 104 s and proposed nothing (exit 0
with the warning); a 14 KB transcript hit the endpoint's own read timeout and the hook exited 1 with
`reflection failed: TimeoutError: timed out`.

## The provider path: sigil inside Hermes

Everything above is the explicit path: the agent is told to search, told to write, and told to run
the hook at the end of a session. That works, and it depends on the agent remembering to do it —
the vault is only as good as the discipline behind it.

Hermes can also drive sigil directly, as a memory provider: `memory.provider: sigil` in the
profile's `config.yaml`. The provider then searches the vault before each answer on its own, and
appends the turns to a session journal under `.sigil/`. Two tools appear in the agent's tool list,
`sigil_search` and `sigil_write`, for the times the agent wants to reach further than the recall
budget allows.

What the provider deliberately does **not** do is write an entry per turn. The journal is a
transcript; entries still come from the promotion pass — `sigil reflect`, `sigil hook`, or an
explicit `sigil write` — and they clear the same gate either way. The append-only failure this
project exists to stop is not something the provider introduces on a new path.

[HERMES.md](HERMES.md) has the install, the vault lookup order, the token budgets and the failure
modes.

## A transcript too long for one prompt

A real session does not fit in one prompt, and pretending it does fails quietly or loudly depending on the
endpoint. Measured against the local 35B on `:8080`: a 3 KB session produced nothing in 104 s, and a 14 KB
one blew the endpoint's own read timeout. So `reflect` loads the transcript by **shape** and sends it
in pieces.

Two shapes are understood:

- **Hermes session JSONL** — one object per line with `role` and `content`, where `content` is a string or a
  list of `{"type": "text", "text": …}` parts. It is rendered as `role: text` lines. Rows that are not
  messages, malformed lines and empty messages are skipped: one corrupt line is not a broken session.
- **Markdown or anything else** — passed through untouched, because a human already chose its shape.

```bash
sigil --vault ~/vault reflect ~/.hermes/profiles/glitch/sessions/<session>.jsonl --max-chars 4000
```

`--max-chars` (default 24000) splits on **turn boundaries only** — no message is ever cut in half. One turn
longer than the cap is kept **whole** as its own chunk rather than truncated, deliberately: silently cutting
a message is the failure this project exists to stop, so bounding an oversized turn is your decision, not
the chunker's quiet one. `--max-chars 0` disables chunking. Joining the chunks reproduces the file exactly.

A chunked pass prints one line per chunk and then the total:

```
chunk 1/4: 2 proposals
chunk 2/4: 1 proposals
...
4 proposals, 3 promoted, 1 left raw
```

Proposals are de-duplicated by summary across chunks — neighbouring context makes the same claim come back
twice, and one claim must stay one entry. `sigil hook` uses the same loader and chunker and takes the same
`--max-chars`; its `--timeout` still bounds the **whole** pass, not each chunk.

### The two timeouts

Two different budgets, and they are not interchangeable. `[llm] timeout` in `.sigil/config.toml` (default
600 s) is the read timeout for **one request** to the model endpoint — raise it when a single chunk is
simply slow. `--endpoint-timeout SECONDS` on `sigil reflect` and `sigil hook` overrides it for that
command only. `sigil hook --timeout` is the other knob entirely: the budget for the **whole pass**, all
chunks together — raise it when the pass as a whole is running long. A value of `0`, a negative number or
a non-integer is refused with an error naming `llm.timeout`.

```toml
[llm]
timeout = 240
```

Size `--max-chars` from the measurement rather than from hope: a 3 KB chunk takes roughly 100 s on the
local 35B, so a 120 s cap fits about one 3 KB chunk and a 600 s cap about 15–20 KB. The default is 600 s
because two minutes is not enough for the sessions this pass exists for — a 12-line slice of a real Hermes
session is 68 KB, and a 178 KB chunk died at 120 s with nothing but `timed out` and an endpoint URL in the
message. A timed-out request now says which knob to turn:

```
LLM request failed: http://127.0.0.1:8080/v1/chat/completions: no response within 600s. One chunk is one
request, so a large chunk needs a longer budget: raise `--endpoint-timeout` (or `[llm] timeout` in
.sigil/config.toml), lower `--max-chars`, or point `[llm] base_url` at a faster endpoint
```

Raise `--endpoint-timeout` when a chunk is slow; raise `hook --timeout` (or shrink `--max-chars`) when the
pass as a whole runs long. Both have to be sized together — a pass of eight chunks at 240 s each still
needs a budget larger than 240 s.

## Exporting

| command | when you want it | what each record carries |
|---|---|---|
| `sigil export sft --out data/sft.jsonl` | supervised fine-tuning examples | `messages` — an optional system prompt (`--system`), then the `ask` as the user turn and summary + evidence + body as the assistant turn — plus `meta` with entry id, kind, state, agent, project, session, created, confidence, importance, source, evidence_kind, evidence_bytes. `--evidence-kind` and `--min-evidence-bytes` narrow the set to entries a skeptic could actually re-check |
| `sigil export dpo --out data/dpo.jsonl` | preference pairs from corrections | `prompt`, `chosen`, `rejected`, and `meta` carrying the new entry, the superseded entry, and the deciding evidence as `reason` |
| `sigil export graph --out data/graph` | the entity/relation graph as data | `nodes.jsonl` (entities, every spelling seen kept as `aliases`) and `edges.jsonl` (src, predicate, dst, note id, confidence, validity window, `live` flag) |

Every record carries provenance, so a dataset can be rebuilt from the vault. Only `promoted`
entries with a usable `ask` become SFT examples; only `supersedes` links become preference pairs.

## When it goes wrong

**No model available.** `sigil reflect <transcript> --fake <file.json>` reads the proposals from
a JSON file instead of calling an endpoint — no network, no model:

```bash
sigil reflect transcript.md --fake response.json
```

```
2 proposals, 2 promoted, 0 left raw
```

The transcript is required. `sigil reflect --fake response.json` on its own exits 2 with
`empty transcript`; the transcript can also be piped in on stdin. `examples/demo.sh` runs this
whole path with no model. Without `--fake`, `reflect` talks to the OpenAI-compatible endpoint in
`.sigil/config.toml` (`[llm] base_url`, default `http://127.0.0.1:8080/v1`), and if nothing is
listening it fails with a message naming the URL it tried — `RuntimeError: LLM request failed:
http://127.0.0.1:8080/v1/chat/completions: <urlopen error [Errno 111] Connection refused>` —
and the command exits non-zero. An HTTP error is reported with its status code, and a body that is
not the expected JSON names the URL too, so a broken endpoint is never mistaken for a model with
nothing to say. `--dry-run` prints the proposals and writes nothing.

**The promotion rate.** `sigil stats` reports `promotion_rate` as promoted ÷ total entries. Near
**1.0** means nearly everything was promoted: you are writing curated entries and barely capturing
— fine for a hand-curated vault, a warning sign for a session-driven one, since the raw layer is
what keeps a corpus clean. Near **0.0** means the gate is rejecting nearly everything: the
reflection pass is producing low-importance proposals, or entries without evidence. Nothing is
wrong with the tool; the proposals are not good enough yet.

**`sigil audit` problems.** It prints a JSON list and exits non-zero when it finds any:
`"2b1a8008: promoted without evidence"`, `"6f97a10b: promoted without evidence"`,
`"6f97a10b: supersedes missing entry deadbeef"`.

- `promoted without evidence` — promoted without proof. Add evidence, or demote to `state=raw`.
- `supersedes missing entry <id>` — that id is not in the vault, so no preference pair is emitted.
- `empty summary` — no summary. It cannot be retrieved usefully and never exports.

**A predicate outside the vocabulary.** Predicates are closed on purpose: `ValueError: predicate
'likes' is not in the vocabulary; add it to <vault>/.sigil/config.toml or pass
allow_new_predicate=True`. Add it to `predicates` in `.sigil/config.toml`, or pass `--allow-new` if
you are sure.

**A malformed `--edge`** — the spec is `SRC:PRED:DST`; `sigil write` says
`--edge wants src:predicate:dst, got 'onlyone:part'` and exits 2.

**A `--edge` that fails after the entry was written.** `sigil write` writes the entry to `notes/`
before it asserts the edges, so a bad predicate leaves the entry on disk but unindexed — `sigil
search` will not find it until `sigil reindex` runs. The index is disposable; the markdown is the
truth, and a reindex rebuilds it in milliseconds.

**A skipped edge at reindex.** A spec in an entry's frontmatter that is malformed, or whose predicate
is outside the vocabulary, is a broken claim — not a reason to lose the entry. The entry is indexed
either way, and `reindex` counts the skips and prints one finding per skipped spec, naming the entry
and the spec so you know which file to open:

```
indexed 873 entries, rebuilt 2 edges (0 closed), 1 edge skipped
  skipped: 41ab77c0: 'not-an-edge' is not a src:predicate:dst spec
```

An out-of-vocabulary predicate is reported the same way: the entry, the spec, and the config file to
add the predicate to. Fix it in the note's frontmatter, or add the predicate to `.sigil/config.toml`
and reindex. A vault with twenty skips prints twenty lines, not one line twenty times.

**An index written by an older version.** It is rebuilt from the markdown, not migrated — the index is
derived, and migrating it once left entity rows whose old display names no longer resolved, so `timeline`
reported an empty graph for a vault whose notes held the edges. You see one line on stderr and nothing in
`notes/` is touched:

```
index schema v0 -> v2: rebuilt from markdown (873 entries, 2 edges)
```

## See also

[DESIGN.md](DESIGN.md) — the layers, the gate, why `ask` is required.
[ARCHITECTURE.md](ARCHITECTURE.md) — module map, data model, extension points.
[MEASUREMENTS.md](MEASUREMENTS.md) — the measured numbers behind the roadmap.
[PLAN.md](../PLAN.md) — what lands next (#2, #3, #5, #9).
