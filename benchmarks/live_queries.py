"""Score retrieval on the ten live-vault questions — with a metric that cannot flatter itself.

The synthetic harness cannot show the live failure: its clusters share a rare marker term with the right
note, so BM25 always has a clean signal. The live corpus is ~870 notes where the words are everywhere,
several notes are pasted transcripts, and the answer is a note whose SUMMARY is the claim — not a note
whose body happens to contain the words.

Four columns, because one number was being quoted for two different questions (#47):

  rank1_gold      rank 1 is one of the notes listed below as a HAND-VERIFIED answer to the question.
                  This is the number to quote. It is 6/10, not 9/10, and the difference is the point.
  rank1_promoted  rank 1 survived the promotion gate rather than arriving raw/pruned.
  rank1_subject   rank 1 mentions the subject of the question (one required stem per query). LOOSE ON
                  PURPOSE, and NOT precision: a hardware audit that happens to say "gateway" scores for
                  "which model is the gateway default". Trend only — never quote it as an accuracy.
  gold_in_5       one of the hand-verified answers is in the top five, even if rank 1 is wrong.

The first row calls the SHIPPED function (`sigil.index.search`). Rows below it re-implement search() to
A/B a candidate shape — and a copy measures itself, so a variant row only means something when the row
that reproduces the shipped settings agrees with the shipped row. The weights the shipped row uses are
read from the package, not retyped here; `benchmarks/tests/test_live_queries.py` pins them.

An empty gold list is an honest statement: the vault does NOT contain an entry that answers the question,
so nothing can rank first for it. Those are curation gaps, not ranking bugs — write the entry (verified,
with evidence) rather than tuning a weight.

    cp -a ~/sigil-vault /tmp/livevault
    .venv/bin/python -m benchmarks.live_queries /tmp/livevault
    .venv/bin/python -m benchmarks.live_queries /tmp/livevault --json out.json --no-variants
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.vault import require_index  # noqa: E402
from sigil.index import COLUMN_WEIGHTS, _fts_query, _fts_query_or, connect, search  # noqa: E402

#: (question, subject stems, hand-verified acceptable answer ids)
#: The gold ids were verified one at a time against the 2026-10-29 copy of ~/quipu-vault; NOTES below
#: says where each answer lives and why two of the lists are empty.
QUERIES = [
    ("why did glitch memory run at 3200 fclk", ("fclk", "3200", "mce"), ("0c0857e6",)),
    ("which model is the gateway default for this team", ("minimax", "model", "gateway"),
     ("f3a6f614",)),
    ("where is the cloudflare tunnel token", ("cloudflared", "tunnel", "token", "cert.pem"),
     ("b14e37ee",)),
    ("what broke with transformers 5.8 and florence", ("florence", "transformers"),
     ("1c8868f3",)),
    ("how do we back up secrets to usb", ("usb", "secret", "backup", "key"),
     ("0058934e",)),
    ("which gpu should i buy for local ai", ("gpu", "vram", "6700", "3090", "4080"), ("2a0a7986",)),
    ("how do we deploy a pump.fun token", ("pump", "deploy", "token"),
     ("f15354cd", "8c2f0d51")),
    ("obs overlay transparency keying", ("obs", "transparen", "chroma", "alpha"),
     ("fb7f85f4", "91af27d6")),
    ("hiveos rig ip addresses for the mining fleet", ("hiveos", "rig", "fleet"),
     ("b2bbf916",)),
    ("did we already try embeddings for search", ("embed", "vector", "hybrid"),
     ("52133f97",)),
]

NOTES = {
    "why did glitch memory run at 3200 fclk":
        "The fact lived only in a raw session blob's body until 2026-10-29, when it was written as its own "
        "entry (`0c0857e6`, evidence re-checked with lscpu + dmidecode over ssh). Before that the promoted "
        "hits were the OTHER box (3900X DOCP 3600, FCLK 1800) and a stale claim that glitch is a 3900X — "
        "both wrong for this machine and worse than a miss, because they read as authoritative.",
    "which gpu should i buy for local ai":
        "Nothing in the corpus answered a purchase question until 2026-10-29, when the standing stance was "
        "written as its own entry (`2a0a7986`, from the local-ai-gpu-selection skill). The subject stems "
        "scored before the entry existed, which is exactly what #47 is about.",
    "how do we deploy a pump.fun token":
        "The launcher-UI note wins rank 1 on the word 'launcher'; the notes that answer 'how' are the "
        "battle-tested launch toolkit and the re-architected Python tx builder.",
    "hiveos rig ip addresses for the mining fleet":
        "The addresses (192.168.32.22/.23/.25) sit in a shell command inside a raw session note, which is "
        "why gold names it; the promoted top hit is a migration plan that mentions the fleet and carries "
        "no addresses at all.",
}

COLUMNS = ("summary", "ask", "evidence", "body")
SHIPPED = tuple(COLUMN_WEIGHTS[c] for c in COLUMNS)
MILD = (4.0, 2.0, 2.0, 1.0)
FLAT = (1.0, 1.0, 1.0, 1.0)


def shipped_row(db, question, limit=5):
    """The one row that measures the code that ships."""
    return search(db, question, limit=limit)


def variant_search(db, query, weights=FLAT, mode="and_thin", demote=None, limit=5, window=4):
    """A local copy of search() for A/B rows only. `shipped_row` is the truth; this is a candidate.

    `demote` multiplies the score of a state. bm25 scores are NEGATIVE distances, so a factor BELOW 1
    makes an entry rank worse and a factor ABOVE 1 makes it rank better.
    """

    def scan(c, match, ws, n):
        sql = ("SELECT e.id, e.state, e.kind, e.summary, bm25(entries_fts, ?, ?, ?, ?) AS score "
               "FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
               "WHERE entries_fts MATCH ? ORDER BY score LIMIT ?")
        return [dict(r) for r in c.execute(sql, list(ws) + [match, n]).fetchall()]

    demote = demote or {}
    c = connect(db)
    match, loose = _fts_query(query), _fts_query_or(query)
    win = max(limit * window, limit)
    try:
        rows = scan(c, match, weights, win)
        if mode == "and_only" and not rows and loose != match:
            rows = scan(c, loose, weights, win)
        elif mode == "and_thin" and loose != match and len(rows) < limit:
            seen = {r["id"] for r in rows}
            rows += [r for r in scan(c, loose, weights, win) if r["id"] not in seen]
    finally:
        c.close()
    for r in rows:
        r["score"] *= demote.get(r["state"], 1.0)
    rows.sort(key=lambda r: r["score"])
    return rows[:limit]


def score(hits_for):
    """Four columns over QUERIES. `hits_for(question)` returns that question's ranked rows."""
    subject = promoted = gold = in5 = 0
    rows = []
    for question, stems, acceptable in QUERIES:
        hits = hits_for(question)
        top = hits[0] if hits else None
        text = ((top["summary"] if top else "") or "").lower()
        ok_subject = bool(top) and any(s in text for s in stems)
        ok_gold = bool(top) and top["id"] in acceptable
        ok_promoted = bool(top) and top["state"] == "promoted"
        ok_in5 = bool(acceptable) and any(h["id"] in acceptable for h in hits)
        subject += ok_subject
        promoted += ok_promoted
        gold += ok_gold
        in5 += ok_in5
        rows.append({
            "question": question,
            "rank1_id": top["id"] if top else None,
            "rank1_state": top["state"] if top else None,
            "rank1_summary": (top["summary"] if top else "")[:160],
            "rank1_subject": ok_subject,
            "rank1_promoted": ok_promoted,
            "rank1_gold": ok_gold,
            "gold_in_5": ok_in5,
            "gold_ids": list(acceptable),
            "note": NOTES.get(question, ""),
        })
    n = len(QUERIES)
    return {"n": n, "rank1_gold": gold, "rank1_promoted": promoted,
            "rank1_subject": subject, "gold_in_5": in5, "rows": rows}


def evaluate(label, db, searcher, **kw):
    report = score(lambda q: searcher(db, q, limit=5, **kw))
    print(f"{label:<44} rank1_gold={report['rank1_gold']}/{report['n']}  "
          f"rank1_promoted={report['rank1_promoted']}/{report['n']}  "
          f"rank1_subject={report['rank1_subject']}/{report['n']}  "
          f"gold_in_5={report['gold_in_5']}/{report['n']}")
    return report


VARIANTS = [
    ("A and_only, flat", dict(weights=FLAT, mode="and_only")),
    ("B and_thin, flat", dict(weights=FLAT, mode="and_thin")),
    ("C and_thin, shipped column weights", dict(weights=SHIPPED, mode="and_thin")),
    ("D and_thin, flat, raw x0.5 pruned x0.1",
     dict(weights=FLAT, mode="and_thin", demote={"raw": 0.5, "pruned": 0.1})),
    ("E and_thin, shipped, raw x0.5 pruned x0.1",
     dict(weights=SHIPPED, mode="and_thin", demote={"raw": 0.5, "pruned": 0.1})),
    ("F and_thin, summary-only (10/1/1/0.2)",
     dict(weights=(10.0, 1.0, 1.0, 0.2), mode="and_thin")),
]


def _run_meta():
    """The provenance a number needs to be re-checkable — same shape as benchmarks/run.py's result file."""
    import platform

    from benchmarks.run import _git_info

    return {"date": "2026-10-29", "python": platform.python_version(),
            "retriever": "sigil.index.search (the shipped function) + a local copy for A/B rows",
            **_git_info()}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = None
    if "--json" in argv:
        i = argv.index("--json")
        if i + 1 >= len(argv):
            print("--json needs a path", file=sys.stderr)
            return 2
        as_json = argv[i + 1]
        del argv[i:i + 2]
    variants = "--no-variants" not in argv
    argv = [a for a in argv if a != "--no-variants"]
    if not argv:
        print("usage: python -m benchmarks.live_queries <vault> [--json out.json] [--no-variants]",
              file=sys.stderr)
        return 2
    db = require_index(Path(argv[0]))
    print(f"corpus: {db}\n")
    report = evaluate("SHIPPED sigil.index.search", db, shipped_row)
    variant_rows = []
    if variants:
        for label, kw in VARIANTS:
            sub = evaluate(label, db, variant_search, **kw)
            variant_rows.append({"variant": label, **{k: sub[k] for k in
                                                      ("rank1_gold", "rank1_promoted", "rank1_subject",
                                                       "gold_in_5", "n")}})
    print()
    for row in report["rows"]:
        if row["rank1_gold"]:
            continue
        verdict = ("NO VERIFIED ANSWER IN CORPUS" if not row["gold_ids"]
                   else f"gold {row['gold_ids']} not at rank 1")
        print(f"  MISS {row['question']!r}\n       rank1: {row['rank1_id']} "
              f"({row['rank1_state']}) {row['rank1_summary'][:90]}\n       {verdict}")
        if row["note"]:
            print(f"       {row['note']}")
    if as_json:
        doc = {
            "dataset": {"name": "live", "version": "v1",
                        "size": None, "queries": report["n"],
                        "notes": "ten hand-judged questions against a copy of ~/sigil-vault; "
                                 "gold ids hand-verified, and an empty list means the corpus has no answer"},
            "run": _run_meta(),
            "metrics": {k: report[k] for k in ("rank1_gold", "rank1_promoted", "rank1_subject", "gold_in_5")},
            "queries": report["rows"],
            "variants": variant_rows,
        }
        Path(as_json).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"\n-> {as_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
