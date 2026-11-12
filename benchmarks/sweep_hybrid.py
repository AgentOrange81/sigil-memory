"""Does a real embedding model fix the vocabulary gap? (#5) — synthetic harness, per query kind.

BM25 saturates the ten live questions (10/10 after curation), so the live set can no longer show a
gain. The discriminating corpus is the synthetic harness, where every remaining BM25 miss is a
`paraphrase` query: the gold note never uses the asker's words.

Sweeps three retrievers over the same 200-note corpus and 100 queries:
  bm25    the shipped search, unmodified
  vector  cosine over nomic-embed-text (llama-server --embeddings on :8083), vector-only ranking
  rrf     reciprocal rank fusion of the two, k=60

    .venv/bin/python -m benchmarks.sweep_hybrid http://127.0.0.1:8083/v1/embeddings nomic-embed
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import benchmarks.run as bench  # noqa: E402
from benchmarks.loaders import load_synthetic  # noqa: E402
from benchmarks.vault import index_db  # noqa: E402
from sigil.embed import RRF_K, RRF_VECTOR_WEIGHT  # noqa: E402
from sigil.index import reindex, search  # noqa: E402
from sigil.store import write_entry  # noqa: E402


def embed(url: str, model: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    batch = 32
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            url, data=json.dumps({"input": texts[i:i + batch], "model": model}).encode(),
            headers={"Content-Type": "application/json"})
        out.extend(d["embedding"] for d in json.load(urllib.request.urlopen(req, timeout=300))["data"])
    return out


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8083/v1/embeddings"
    model = sys.argv[2] if len(sys.argv) > 2 else "nomic-embed"
    dataset = load_synthetic()
    ks = [5, 10]

    with tempfile.TemporaryDirectory(prefix="sigil-hybrid-") as tmp:
        root = Path(tmp) / "vault"
        root.mkdir(parents=True)
        for e in dataset.entries:
            write_entry(root, e)
        db = index_db(root)
        reindex(root, db)

        texts = [f"{e.summary}\n{e.body}"[:1500] for e in dataset.entries]
        vecs = {e.id: unit(v) for e, v in zip(dataset.entries, embed(url, model, texts), strict=False)}
        query_texts = [q.text for q in dataset.queries]
        qvecs = {q.qid: unit(v) for q, v in zip(dataset.queries,
                                                embed(url, model, query_texts), strict=False)}

        def vector_rank(qid: str, limit: int) -> list[str]:
            qv = qvecs[qid]
            scored = [(sum(a * b for a, b in zip(qv, v, strict=False)), eid) for eid, v in vecs.items()]
            scored.sort(reverse=True)
            return [eid for _s, eid in scored[:limit]]

        def bm25_rank(qid: str, text: str, limit: int) -> list[str]:
            return [r["id"] for r in search(db, text, limit=limit)]

        def rrf_rank(qid: str, text: str, limit: int, w_vec: float = 0.5) -> list[str]:
            """Weighted reciprocal rank fusion: w_vec on the vector ranking, the rest on bm25.
            RRF is scale-free (no need to compare a bm25 distance with a cosine), and the one knob is
            how much the lexical ranking is allowed to veto a semantic one."""
            fused: dict[str, float] = {}
            for lst, w in ((bm25_rank(qid, text, 60), 1.0 - w_vec), (vector_rank(qid, 60), w_vec)):
                for rank, eid in enumerate(lst, 1):
                    fused[eid] = fused.get(eid, 0.0) + w / (RRF_K + rank)
            return [eid for eid, _s in sorted(fused.items(), key=lambda kv: -kv[1])[:limit]]

        rankings = {
            "bm25 (shipped)": {q.qid: bm25_rank(q.qid, q.text, max(ks)) for q in dataset.queries},
            "vector only": {q.qid: vector_rank(q.qid, max(ks)) for q in dataset.queries},
        }
        for w in (0.5, 0.65, 0.8, 0.9):
            rankings[f"rrf vec={w}"] = {q.qid: rrf_rank(q.qid, q.text, max(ks), w)
                                        for q in dataset.queries}

    print(f"{'retriever':<16}{'R@5':>7}{'R@10':>7}{'MRR':>7}   per-kind R@5 (para / phrase / quest / rare)")
    metrics: dict[str, dict] = {}
    for label, ranked in rankings.items():
        m = bench._metrics(dataset.queries, ranked, ks)
        metrics[label] = m
        kinds = m["per_kind"]
        print(f"{label:<16}{m['recall_at_5']:>7.3f}{m['recall_at_10']:>7.3f}{m['mrr']:>7.3f}   "
              + " / ".join(f"{kinds[k]['recall_at_5']:.2f}"
                           for k in ("paraphrase", "phrase", "question", "rare")))
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if out:
        from datetime import datetime, timezone
        payload = {
            "dataset": {"name": dataset.name, "version": dataset.version,
                        "size": len(dataset.entries), "queries": len(dataset.queries)},
            "run": {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "embed_url": url, "embed_model": model,
                    "rrf_k": RRF_K, "rrf_vector_weight": RRF_VECTOR_WEIGHT},
            "k": ks,
            "query_set_sha256": bench.query_set_sha256(dataset.queries),
            "corpus_sha256": bench.corpus_sha256(dataset.entries),
            "metrics": metrics,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
