"""Optional semantic retrieval: embeddings from a local HTTP server, fused with BM25 (#5).

The package still has zero runtime dependencies: this module talks to any OpenAI-compatible
`/v1/embeddings` endpoint with `urllib` and stores the vectors in the SQLite index. There is no model
inside sigil, no torch, no numpy — point `embed_url` at llama.cpp (`llama-server --embeddings`), at
ollama, or at anything else that speaks the shape.

Why fused rather than vector-only, measured on the synthetic harness (100 queries, 200 notes,
nomic-embed-text-v1.5 f16 on CPU, 2026-10-29):

    retriever        R@5    R@10    MRR    para/phrase/question/rare R@5
    bm25 (today)     0.640  0.900  0.318   0.48 / 1.00 / 0.52 / 0.56
    vector only      0.770  0.920  0.393   0.56 / 0.96 / 0.80 / 0.76
    rrf vec=0.8      0.760  0.910  0.383   0.56 / 1.00 / 0.76 / 0.72

Vector-only has the best headline number and gives up exact-phrase retrieval (1.00 -> 0.96: a phrase
query is the one shape where lexical matching is simply right). Reciprocal rank fusion at 0.8 keeps
phrase at 1.00 and costs 0.01 R@5 against vector-only, which is the trade this module takes.

Vectors are DERIVED data, like the FTS index: they live in `.sigil/index.db` and are rebuilt with
`sigil embed`. Losing the index costs a re-embed, never a memory. When no vectors are present,
`search --hybrid` degrades to the lexical ranking instead of failing.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import urllib.error
import urllib.request

#: Reciprocal rank fusion constant. 60 is the value from the original RRF paper and behaves well
#: enough that tuning it was not worth a second measurement.
RRF_K = 60

#: How much of the fusion comes from the vector ranking (the rest is BM25). Measured: 0.5 buries the
#: semantic signal under lexical noise, 0.9 loses exact-phrase precision, 0.8 keeps phrase R@5 at
#: 1.00 while taking overall R@5 from 0.640 to 0.760.
RRF_VECTOR_WEIGHT = 0.8

#: Candidates taken from each ranking before fusing, per requested result.
RRF_WINDOW = 60

#: The text that gets embedded for an entry. The summary is the retrieval handle and `ask` is the
#: question it answers; the body is truncated because it mostly adds cost, not signal.
EMBED_BODY_CHARS = 600


def entry_text(summary: str, ask: str, body: str = "") -> str:
    parts = [(summary or "").strip(), (ask or "").strip()]
    if body:
        parts.append(body.strip()[:EMBED_BODY_CHARS])
    return "\n".join(p for p in parts if p)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _post(url: str, model: str, texts: list[str], timeout: float) -> list[list[float]]:
    req = urllib.request.Request(
        url, data=json.dumps({"input": texts, "model": model}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    data = payload.get("data") or []
    if len(data) != len(texts):
        raise RuntimeError(f"embedding endpoint returned {len(data)} vectors for {len(texts)} inputs")
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in ordered]


def embed_texts(url: str, model: str, texts: list[str], batch: int = 32,
                timeout: float = 120.0, transport=None) -> list[list[float]]:
    """Embed `texts`, one HTTP round-trip per `batch`. `transport` is injectable for hermetic tests."""
    send = transport or (lambda batch_texts: _post(url, model, batch_texts, timeout))
    out: list[list[float]] = []
    for i in range(0, len(texts), max(1, batch)):
        out.extend(send(texts[i:i + max(1, batch)]))
    return out


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def pack(vector: list[float]) -> bytes:
    return json.dumps([round(x, 6) for x in vector]).encode("utf-8")


def unpack(blob: bytes) -> list[float]:
    return json.loads(blob.decode("utf-8"))


# --- storage (derived, in the disposable index) ---------------------------------------------


def ensure_schema(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS embeddings(
      entry_id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL,
      digest TEXT NOT NULL, vec BLOB NOT NULL)""")


def store(c: sqlite3.Connection, model: str, vectors: dict[str, tuple[str, list[float]]]) -> int:
    """Write {entry_id: (text_digest, vector)}. Returns how many rows were written."""
    ensure_schema(c)
    rows = [(eid, model, len(vec), digest, pack(vec)) for eid, (digest, vec) in vectors.items()]
    with c:  # commit: the caller owns the connection, and an uncommitted vector is a silent no-op
        c.executemany("INSERT OR REPLACE INTO embeddings(entry_id, model, dim, digest, vec) "
                      "VALUES(?,?,?,?,?)", rows)
    return len(rows)


def stored_digests(c: sqlite3.Connection, model: str) -> dict[str, str]:
    ensure_schema(c)
    return {r[0]: r[1] for r in c.execute(
        "SELECT entry_id, digest FROM embeddings WHERE model = ?", (model,))}


def load(c: sqlite3.Connection, model: str | None = None) -> dict[str, list[float]]:
    ensure_schema(c)
    if model:
        rows = c.execute("SELECT entry_id, vec FROM embeddings WHERE model = ?", (model,)).fetchall()
    else:
        rows = c.execute("SELECT entry_id, vec FROM embeddings").fetchall()
    return {r[0]: unpack(r[1]) for r in rows}


def vector_ranking(query_vec: list[float], vectors: dict[str, list[float]],
                   limit: int) -> list[str]:
    """Cosine similarity, best first. Brute force on purpose: a vault is thousands of rows, and an
    ANN index would be a second thing to keep consistent with the markdown."""
    q = unit(query_vec)
    scored = [(sum(a * b for a, b in zip(q, unit(vec), strict=False)), eid) for eid, vec in vectors.items()]
    scored.sort(key=lambda pair: -pair[0])
    return [eid for _score, eid in scored[:limit]]


def hybrid_search(db, query: str, url: str, model: str, limit: int = 10,
                  transport=None, **filters) -> tuple[list[dict], str]:
    """Fused retrieval over the shipped lexical ranking and stored vectors.

    Returns (rows, note) where `note` is "" on success or the reason the caller got lexical results:
    an empty vault of vectors is not an error — it is a vault that has not been embedded yet, and the
    agent should still get an answer.
    """
    from . import index as index_mod  # local import: index imports nothing from here

    lexical = index_mod.search(db, query, limit=RRF_WINDOW, **filters)
    c = index_mod.connect(db)
    try:
        vectors = load(c, model)
    finally:
        c.close()
    if not vectors:
        return lexical[:limit], f"no vectors for model {model!r}: run `sigil embed` to build them"
    if not url:
        return lexical[:limit], "no embed_url configured: set [embed] url in .sigil/config.toml"
    query_vec = embed_texts(url, model, [query], transport=transport)[0]
    semantic = vector_ranking(query_vec, vectors, RRF_WINDOW)
    order = fuse([r["id"] for r in lexical], semantic)

    by_id = {r["id"]: r for r in lexical}
    c = index_mod.connect(db)
    try:
        missing = [eid for eid in order[:limit] if eid not in by_id]
        for eid in missing:
            row = c.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
            if row is None:
                continue
            by_id[eid] = dict(index_mod.row_to_entry(row).__dict__, id=eid, snip="")
    finally:
        c.close()
    rows = [by_id[eid] for eid in order[:limit] if eid in by_id]
    return rows, ""


def fuse(lexical: list[str], semantic: list[str], k: int = RRF_K,
         w_vector: float = RRF_VECTOR_WEIGHT) -> list[str]:
    """Weighted reciprocal rank fusion. Scale-free: a bm25 distance and a cosine never need to be
    compared, only their ranks."""
    scores: dict[str, float] = {}
    for ranking, weight in ((lexical, 1.0 - w_vector), (semantic, w_vector)):
        for rank, eid in enumerate(ranking, 1):
            scores[eid] = scores.get(eid, 0.0) + weight / (k + rank)
    return [eid for eid, _s in sorted(scores.items(), key=lambda kv: -kv[1])]
