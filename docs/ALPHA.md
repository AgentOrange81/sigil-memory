# Alpha

**This is a snapshot at 0.1.0, not today's state.** Every number below was measured then; the vault has
grown several times since and several gaps listed here have shipped (`sigil embed`, `sigil mcp`). Treat
it as a record of what alpha looked like, and use `sigil stats` / `sigil audit` for the current figures.

What "alpha" means here, measured on the live vault. Every number below was produced by running the
command shown, on this box, against `~/sigil-vault`. Where a figure comes from an issue instead, the
issue says so.

## 1. What alpha means here

Alpha means **one vault is in daily use** — not "tests pass". The corpus is real and the search hits
are read before answering.

| | |
|---|---|
| vault | `~/sigil-vault` — 830 entries, 830 markdown files in `notes/`, 0 in `entities/` |
| where it came from | 829 entries imported from `~/fabric` in 355 ms (recorded in the migration entry `51375dda`); the 830th was written by hand during that migration (`source: cli`) |
| size | 5.3 MiB of files (7.3 MB allocated on disk), of which the SQLite index is 3.3 MB |
| CLI | `sigil` on `PATH` at 0.1.0 (`/home/dirk/.local/bin/sigil`), so every profile and every shell gets it |

Alpha is **not** production-hardened. `sigil --vault ~/sigil-vault audit` currently reports **509
problems**, all of one kind (`promoted without evidence`), and exits 1. §4 says what that means for
trusting a result.

## 2. The two habits

**Recall before answering.** Run `sigil --vault ~/sigil-vault search "<topic>"` *before* you answer
from memory. The topic query is the working case: `search "DOCP 3200"` returns 3 hits, `"pump.fun"`
10, `"cloudflare tunnel"` 10, `"prisma migrate"` 10. The query itself is sub-millisecond (median
0.55–0.78 ms over 20 reps); the 50 ms quoted in #28 is the whole warm CLI call. Do not skip the
search because the answer feels obvious — the corpus holds decisions you did not make, and a wrong
answer costs more than a 60 ms command.

**Write one entry per real outcome.** When something is decided, fixed, or disproved, write it once:
`sigil --vault ~/sigil-vault write --kind decision --summary "<the claim>" --evidence "<the proof: a
number, a path, a command, a quote>" --ask "<the question this answers>"`. One entry per outcome, not
one per turn. The summary is the retrieval handle, so write it as a statement, not a topic. The
evidence is what makes the entry promotable and what lets a skeptic re-check it — an entry without
evidence stays raw, and the importer's state-lie (§4) is what happens when it is skipped.

## 3. What works today

- **Recall over 830 entries.** Topic queries hit reliably; §2 has the four measured examples.
- **The lifecycle and the gate.** `capture` → `reflect` → `promote` → `correct` → `export`. The
  promotion gate is `importance >= promote_threshold` (0.5 in `.sigil/config.toml`) **and** non-empty
  evidence; below either, the entry stays raw (`sigil/promote.py:148`).
- **Supersede produces a preference pair.** `write --supersedes <id>` on an entry that has an `ask`
  and evidence yields a DPO pair (chosen = the correction, rejected = the superseded claim). Verified
  on a scratch vault: 1 pair, `preference_pairs: 1`.
- **Export works.** `export sft` / `export dpo` / `export graph` write JSONL. On the live vault they
  produce **1 SFT example, 0 preference pairs, 0 nodes, 0 edges** — §4 says why the counts are
  near-zero.
- **`rm .sigil/index.db` + `reindex` reproduces the entries.** Verified on a copy of the live vault:
  `indexed 830 entries`, 830 note files still on disk, all 830 still searchable, `stats` identical
  before and after. Markdown is the truth for entries; the index is derived. **The graph is not**
  (#24).
- **Install and import.** `sigil import fabric ~/fabric` into a fresh vault: 838 written, 16 skipped,
  0.29 s wall. `~/fabric` is still growing, so re-run it for today's counts. Re-importing into the
  same vault produces the same notes and the same counts — the import is idempotent.

## 4. What does not, with the issue number

**#25 — question-shaped queries return 0 hits.** `"why did we drop DOCP to 3200"`,
`"how do I fix the memory instability"`, `"what did we decide about DOCP"` → 0 hits, all three. Every
token is ANDed. Ask a question and you get an empty page, and will wrongly conclude the vault is
empty. Search in topic words, not questions.

**#26 — `state` lies after import.** 509 of the 781 promoted entries have no evidence (`audit`).
The importer maps fabric's `training_value` straight to `promoted`, skipping the gate. Treat
`state: promoted` as inherited, not verified.

**#30 — pasted tool transcripts rank as trusted statements.** The rank-1 hit for `"DOCP 3200"` is
entry `8b786d00`, a pasted transcript beginning ` ``` FILES: … `, marked `promoted decision`. The top
hit can be worthless.

**#31 — rank-1 precision is unmeasured and poor.** BM25 ignores *where* a term matched: a hit in
`summary` scores the same as one buried in `body`. Ties are often resolved by the wrong entry.

**#24 — the graph is empty and lives only in SQLite.** The live vault has **0 edges and 0 entity
files**: not one imported note carries an `edges:` assertion, so the graph is exactly as good as the
`--edge` assertions written by hand. It is also not derivable — on a scratch vault with one edge,
`rm .sigil/index.db` + `reindex` left `timeline trx40` **empty**.

**#27 — imported history produces no training data.** `export sft` needs a self-contained `ask`
(≥20 chars, ≥3 words) and the corpus has one such entry in 830 — the migration record itself, written
by hand. Imported fabric entries have no `ask` field, so they yield nothing. Recall, not training
data; the SFT set starts from entries written or reflected after the migration.

**#20 — superseding a fact left the old graph edge live.** Verified at this tag: after
`write --supersedes`, `timeline` shows *both* edges `→ now`. **Fixed after the tag** (see CHANGELOG
Unreleased): the write path closes the superseded entry's live edges, records the closure in the
markdown, and `sigil audit` reports any superseded entry whose edge is still live — the shape a vault
written by this version is in.

**#23 — a vault written before the identity fix cannot be opened at all.** Reproduced on a synthetic
old schema: `sqlite3.OperationalError: no such column: key` from `sigil/index.py:43` — the
`CREATE INDEX` runs before the `ALTER TABLE` migration, so the migration is unreachable exactly when
it is needed. Every command dies on such a vault.

**#5 — no embeddings.** At 0.1.0 there was no `vec` table and no `sigil embed` subcommand, so retrieval
was BM25 lexical only. `sigil embed` and `search --hybrid` have shipped since; a vault with no `[embed]
url` still runs lexical-only, which is the default.

**#9 — no MCP server.** At 0.1.0 there was no `sigil mcp` subcommand; it has since shipped, so the
integration surface is now CLI + MCP + the provider. The `quipu` agent skill keeps its old name for now.

Two limits on the above: `audit` only sees the problems it checks (evidence), and cannot detect the
#30 transcript junk; and the #31 precision figures come from the benchmark harness on synthetic
data, not from this vault.

## 5. Data you may lose, and how to recover

**Entries are safe.** The markdown in `notes/` is the truth; the SQLite index is derived. Delete
`.sigil/index.db` and `sigil reindex` rebuilds every entry and every search hit (verified above).

**The graph is not safe.** It exists only in the index, and `reindex` does not rebuild it (#24).
Asserting edges and then deleting the index loses them, silently. Back up `.sigil/index.db` if you
have graph edges worth keeping.

**The old corpus is archived.** `~/memory-archive/fabric-full-20261028.tar.zst` (1.4 MB), with
`sha256sum -c` verified `OK`. `~/memory-vault` is the older Obsidian/`mvault` build — the readable
archive, and **must not be written to** (it belongs to a different CLI; two tools on one directory is
how you corrupt a vault). It is *not* filesystem-read-only, so that rule is one you follow, not one
the OS enforces.

## 6. Upgrading and re-importing

The live vault was imported with an importer that still has **#26** and **#30**. Until those land, a
re-import is idempotent but does **not** correct the state: re-running
`sigil import fabric ~/fabric` into a scratch copy left its audit at 515 problems, unchanged from
before the re-import. The claims about correcting state apply once #26 and #30 are fixed; today they
do not hold.

**How to tell whether a vault needs it:** run `sigil --vault ~/sigil-vault audit` and read
`problems`. Today the live vault reports 509 problems, all `promoted without evidence`. To re-import:
take a copy of the vault, run `sigil --vault <copy> import fabric ~/fabric`, then `audit` the copy
and compare. Never re-import over the live vault to test a change.

Related: [PLAN.md](PLAN.md) for the milestones; [USING.md](USING.md) for the day-to-day commands;
[MEASUREMENTS.md](MEASUREMENTS.md) for the measured costs behind the roadmap.
