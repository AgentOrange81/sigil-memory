"""Run a retrieval benchmark and write the result file.

The retrieval path is the shipped one, end to end: notes are written as markdown through
`sigil.store.write_entry`, indexed by `sigil.index.reindex` (SQLite FTS5), and queried with
`sigil.index.search` (bm25). Nothing is re-implemented here, so the number is the number the
product would give an agent.

    python -m benchmarks.run --dataset synthetic --k 5 --k 10 --json out.json

Exit codes: 0 for a completed run, 0 for a skipped dataset (nothing to run, nothing claimed),
1 for a failed run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sigil.index import reindex, search
from sigil.store import write_entry

from .loaders import (
    BenchQuery,
    Dataset,
    corpus_sha256,
    load_longmemeval,
    load_synthetic,
    query_set_sha256,
)
from .vault import index_db

REPO_ROOT = Path(__file__).resolve().parent.parent
RETRIEVER = "sigil.index.search (sqlite FTS5, bm25, unicode61+porter)"


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_info() -> dict:
    rev = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    return {"git_rev": rev or "unknown", "git_dirty": dirty}


def _index_corpus(dataset: Dataset, root: Path) -> Path:
    """Write the corpus as markdown and index it with the real index."""
    for e in dataset.entries:
        write_entry(root, e)
    db = index_db(root)
    reindex(root, db)
    return db


def _rank(query: BenchQuery, db: Path, limit: int) -> list[str]:
    """The entry ids the shipped search returns for this query, best first."""
    return [row["id"] for row in search(db, query.text, limit=limit)]


def _metrics(queries: list[BenchQuery], ranked: dict[str, list[str]], ks: list[int]) -> dict:
    """Recall@k and MRR over the retrieved ranking.

    recall@k  mean over queries of |top-k hits ∩ gold| / |gold|
    mrr       mean over queries of 1 / rank of the FIRST gold hit in the retrieved list
              (the list is capped at max(k); a gold entry outside it scores 0)
    """
    max_k = max(ks)
    per_kind_hits: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    per_kind_rr: dict[str, list[float]] = defaultdict(list)
    recalls = {k: [] for k in ks}
    rrs = []
    for q in queries:
        got = ranked.get(q.qid, [])
        gold = set(q.gold)
        for k in ks:
            hits = len(gold & set(got[:k]))
            score = hits / len(gold) if gold else 0.0
            recalls[k].append(score)
            per_kind_hits[q.kind][k].append(score)
        rr = 0.0
        for i, eid in enumerate(got[:max_k], start=1):
            if eid in gold:
                rr = 1.0 / i
                break
        rrs.append(rr)
        per_kind_rr[q.kind].append(rr)
    out: dict = {
        "mrr": round(sum(rrs) / len(rrs), 4) if rrs else 0.0,
        "queries": len(queries),
    }
    for k in ks:
        out[f"recall_at_{k}"] = round(sum(recalls[k]) / len(recalls[k]), 4) if recalls[k] else 0.0
    out["per_kind"] = {
        kind: {
            "queries": len(per_kind_rr[kind]),
            **{
                f"recall_at_{k}": round(sum(hits) / len(hits), 4) if hits else 0.0
                for k, hits in sorted(per_kind_hits[kind].items())
            },
            "mrr": round(sum(per_kind_rr[kind]) / len(per_kind_rr[kind]), 4),
        }
        for kind in sorted(per_kind_hits)
    }
    return out


def _misses(queries: list[BenchQuery], ranked: dict[str, list[str]], max_k: int,
            summaries: dict[str, str]) -> list[dict]:
    """Every query whose answer was not retrieved - the failures are published, not hidden."""
    out = []
    for q in queries:
        got = ranked.get(q.qid, [])
        found = [i for i, eid in enumerate(got[:max_k], start=1) if eid in set(q.gold)]
        if found and len(found) == len(q.gold):
            continue
        out.append({
            "qid": q.qid,
            "kind": q.kind,
            "query": q.text,
            "gold": list(q.gold),
            "gold_summary": summaries.get(q.gold[0], "") if q.gold else "",
            "found_at": found[0] if found else None,
            "retrieved": [
                {"rank": i, "id": eid, "summary": summaries.get(eid, "")}
                for i, eid in enumerate(got[:max_k], start=1)
            ],
        })
    return out


def run(dataset: Dataset, ks: list[int]) -> tuple[dict, dict[str, list[str]]]:
    """Index the corpus in a temp vault, query it, and score the answers."""
    with tempfile.TemporaryDirectory(prefix="sigil-bench-") as tmp:
        root = Path(tmp) / "vault"
        root.mkdir(parents=True, exist_ok=True)
        db = _index_corpus(dataset, root)
        max_k = max(ks)
        ranked = {q.qid: _rank(q, db, max_k) for q in dataset.queries}
        summaries = {e.id: e.summary for e in dataset.entries}
        metrics = _metrics(dataset.queries, ranked, ks)
        misses = _misses(dataset.queries, ranked, max_k, summaries)
    result = {
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "size": len(dataset.entries),
            "queries": len(dataset.queries),
            "notes": dataset.notes,
        },
        "run": {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "python": sys.version.split()[0],
            "retriever": RETRIEVER,
            **_git_info(),
        },
        "k": sorted(ks),
        "query_set_sha256": query_set_sha256(dataset.queries),
        "corpus_sha256": corpus_sha256(dataset.entries),
        "metrics": metrics,
        "misses": misses,
    }
    return result, ranked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="benchmarks.run", description="sigil retrieval benchmark")
    ap.add_argument("--dataset", required=True, choices=["synthetic", "longmemeval"])
    ap.add_argument("--k", action="append", type=int, default=None,
                    help="cut-off to score (repeatable; default 5 and 10)")
    ap.add_argument("--json", dest="out", default=None, help="where to write the result file")
    ap.add_argument("--path", default=None, help="dataset file (longmemeval only)")
    args = ap.parse_args(argv)

    ks = sorted(set(args.k)) if args.k else [5, 10]
    if args.dataset == "synthetic":
        dataset = load_synthetic()
    else:
        if not args.path:
            print("dataset not present, skipping")
            return 0
        dataset = load_longmemeval(args.path)
        if dataset is None:
            print("dataset not present, skipping")
            return 0

    result, _ranked = run(dataset, ks)
    m = result["metrics"]
    print(f"dataset: {result['dataset']['name']} {result['dataset']['version']} "
          f"({result['dataset']['size']} entries, {result['dataset']['queries']} queries)")
    for k in result["k"]:
        print(f"R@{k}: {m[f'recall_at_{k}']:.4f}")
    print(f"MRR: {m['mrr']:.4f}")
    for kind, stats in m["per_kind"].items():
        label = f"recall_at_{result['k'][0]}"
        print(f"  {kind}: n={stats['queries']} R@{result['k'][0]}={stats[label]:.4f} "
              f"MRR={stats['mrr']:.4f}")
    print(f"misses: {len(result['misses'])}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
