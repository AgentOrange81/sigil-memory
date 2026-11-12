"""Sweep retrieval variants against the shipped benchmark harness (#31, #43).

Not shipped: a dev instrument. `benchmarks.run` imports `search` by name, so a variant is installed
by rebinding `benchmarks.run.search`; the corpus, the index and the scoring are the real ones.

Variants:
  and_only      the current shape: AND, OR only when the AND set is EMPTY
  and_thin      AND first; append OR hits when the AND set is shorter than the limit
  union_boost   candidates from both shapes, all-token matches multiplied by `boost`
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import benchmarks.run as bench  # noqa: E402
from benchmarks.loaders import load_synthetic  # noqa: E402
from sigil.index import _fts_query, _fts_query_or, connect  # noqa: E402

WEIGHT_SETS = {
    "flat": (1.0, 1.0, 1.0, 1.0),
    "mild": (4.0, 2.0, 2.0, 1.0),
    "shipped": (10.0, 6.0, 3.0, 1.0),
    "sharp": (20.0, 10.0, 4.0, 1.0),
    "summary_only": (10.0, 1.0, 1.0, 0.2),
    "ask_heavy": (8.0, 8.0, 2.0, 1.0),
    "mid": (10.0, 4.0, 2.0, 0.5),
    "mid2": (10.0, 3.0, 1.0, 0.5),
}


def _scan(c, match, weights, filters, params, limit):
    clauses = ["entries_fts MATCH ?"] + filters
    sql = ("SELECT e.*, bm25(entries_fts, ?, ?, ?, ?) AS score, "
           "snippet(entries_fts, 0, '<<', '>>', '…', 14) AS snip "
           "FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
           "WHERE " + " AND ".join(clauses) + " ORDER BY score LIMIT ?")
    return [dict(r) for r in c.execute(sql, list(weights) + [match] + params + [limit]).fetchall()]


def make_search(mode, weights, boost=0.5, window=4, demote=None):
    def search(db, query, kind=None, state=None, agent=None, project=None, limit=10):
        c = connect(db)
        match, loose = _fts_query(query), _fts_query_or(query)
        if not match:
            c.close()
            return []
        filters, params = [], []
        for col, val in (("kind", kind), ("state", state), ("agent", agent), ("project", project)):
            if val:
                filters.append(f"e.{col} = ?")
                params.append(val)
        win = max(limit * window, limit)
        try:
            rows = _scan(c, match, weights, filters, params, win)
            if mode == "and_only" and not rows and loose != match:
                rows = _scan(c, loose, weights, filters, params, win)
            elif mode == "and_thin" and loose != match and len(rows) < limit:
                seen = {r["id"] for r in rows}
                extra = [r for r in _scan(c, loose, weights, filters, params, win)
                         if r["id"] not in seen]
                rows = rows + extra
            elif mode == "union_boost" and loose != match:
                byid = {r["id"]: r for r in _scan(c, loose, weights, filters, params, win)}
                for row in rows:
                    hit = dict(row)
                    hit["score"] = row["score"] * boost
                    byid[hit["id"]] = hit
                rows = list(byid.values())
        finally:
            c.close()
        for row in rows:
            row["score"] *= (demote or {}).get(row["state"], 1.0)
        rows.sort(key=lambda r: r["score"])
        return rows[:limit]
    return search


def score(dataset, search, ks=(5, 10)):
    bench.search = search
    result, _ = bench.run(dataset, list(ks))
    m = result["metrics"]
    kinds = {k: v["recall_at_5"] for k, v in m["per_kind"].items()}
    return m["recall_at_5"], m["recall_at_10"], m["mrr"], kinds


def main():
    dataset = load_synthetic()
    rows = []
    variants = [("and_only", "flat", None)]
    for name in WEIGHT_SETS:
        if name != "flat":
            variants.append(("and_only", name, None))
    for name in ("mild", "shipped", "sharp", "summary_only", "ask_heavy", "mid", "mid2"):
        variants.append(("and_thin", name, None))
    for name, boost in itertools.product(("mild", "shipped", "sharp"), (0.5, 0.25, 0.1)):
        variants.append(("union_boost", name, boost))

    variants.append(("and_thin", "shipped", "demote"))
    variants.append(("and_thin", "mild", "demote"))
    for mode, wname, boost in variants:
        kw = {"demote": {"raw": 0.5, "pruned": 0.1}} if boost == "demote" else {}
        r5, r10, mrr, kinds = score(dataset, make_search(mode, WEIGHT_SETS[wname],
                                                         boost if boost != "demote" else 0.5, **kw))
        label = (f"{mode:<12} {'w=' + wname:<8}"
                 + (f" boost={boost}" if boost and boost != "demote" else "")
                 + (" demote" if boost == "demote" else ""))
        rows.append((r5, label, r10, mrr, kinds))
        print(f"{label:<32} R@5={r5:.3f} R@10={r10:.3f} MRR={mrr:.3f} "
              + " ".join(f"{k[:4]}={v:.2f}" for k, v in sorted(kinds.items())))
    print("\nbest by R@5:")
    for r5, label, r10, mrr, _kinds in sorted(rows, reverse=True)[:5]:
        print(f"  {label:<32} R@5={r5:.3f} R@10={r10:.3f} MRR={mrr:.3f}")


if __name__ == "__main__":
    main()
