"""Optional semantic retrieval (#5): storage, fusion and the degradation path — no network.

The embedding endpoint is injected as a `transport`, so these tests never leave the process: a fake
transport returns a deterministic vector derived from the text's words, which is enough to test the
ranking, the fusion and the plumbing without an embedding model on the runner.

What is pinned:
  - vectors round-trip through the disposable index and a re-embed skips unchanged entries;
  - a hybrid search returns fused rows whose metadata comes from the entries table (a vector-only hit
    the lexical ranking never saw must still print its summary);
  - with no vectors, or with no endpoint, hybrid DEGRADES to the lexical ranking and says why —
    it must never fail, and never silently pretend to be semantic;
  - the fusion weights what was measured: the lexical ranking still owns exact-phrase queries.
"""


import pytest

from sigil import config as config_mod
from sigil import embed as embed_mod
from sigil import index as index_mod
from sigil.store import Entry, write_entry


def fake_transport(texts):
    """A deterministic stand-in for an embedding model: bag-of-words hashed into 8 dims."""
    out = []
    for text in texts:
        vec = [0.0] * 8
        for word in (text or "").lower().split():
            vec[sum(word.encode()) % 8] += 1.0
        out.append(vec)
    return out


@pytest.fixture()
def vault(tmp_path):
    cfg = config_mod.load(tmp_path / "vault", create=True)
    entries = [
        Entry(kind="decision", state="promoted", summary="Markdown is the source of truth",
              evidence="40 ms full reindex", ask="where do notes live?",
              body="sqlite is a cache and can be deleted"),
        Entry(kind="lesson", state="promoted",
              summary="Wan 2.2 video VAE decode hangs the ring on gfx1031",
              evidence="journalctl -b -1", ask="why did the video decode stall?"),
        Entry(kind="fact", state="promoted", summary="Docker volumes live under /var/lib/docker",
              evidence="ls", ask="where are docker volumes?"),
    ]
    for e in entries:
        write_entry(cfg.root, e)
    index_mod.reindex(cfg.root, cfg.db_path)
    return cfg, entries


def embed_vault(cfg, model="fake"):
    c = index_mod.connect(cfg.db_path)
    try:
        vectors = {}
        for _p, e in __import__("sigil.store", fromlist=["iter_entries"]).iter_entries(cfg.root):
            text = embed_mod.entry_text(e.summary, e.ask, e.body)
            vectors[e.id] = (embed_mod.text_digest(text), fake_transport([text])[0])
        return embed_mod.store(c, model, vectors)
    finally:
        c.close()


def test_vectors_round_trip_and_are_skipped_when_unchanged(vault):
    cfg, entries = vault
    assert embed_vault(cfg) == len(entries)
    c = index_mod.connect(cfg.db_path)
    try:
        have = embed_mod.stored_digests(c, "fake")
        assert set(have) == {e.id for e in entries}
        loaded = embed_mod.load(c, "fake")
        assert len(loaded[entries[0].id]) == 8
    finally:
        c.close()


def test_hybrid_returns_metadata_for_a_vector_only_hit(vault):
    cfg, entries = vault
    embed_vault(cfg)
    rows, note = embed_mod.hybrid_search(
        cfg.db_path, "video decode stall gfx1031", "http://unused", "fake", limit=3,
        transport=fake_transport)
    assert note == "", note
    assert rows, "hybrid returned nothing"
    assert all("summary" in r and r["summary"] for r in rows), rows
    assert {r["id"] for r in rows} <= {e.id for e in entries}


def test_hybrid_degrades_to_lexical_without_vectors(vault):
    cfg, _entries = vault
    rows, note = embed_mod.hybrid_search(cfg.db_path, "markdown source of truth",
                                         "http://unused", "fake", limit=3,
                                         transport=fake_transport)
    assert "no vectors" in note, note
    lexical = index_mod.search(cfg.db_path, "markdown source of truth", limit=3)
    assert [r["id"] for r in rows] == [r["id"] for r in lexical]


def test_hybrid_degrades_without_an_endpoint(vault):
    cfg, _entries = vault
    embed_vault(cfg)
    rows, note = embed_mod.hybrid_search(cfg.db_path, "markdown", "", "fake", limit=3,
                                         transport=fake_transport)
    assert "embed_url" in note, note
    assert rows, "lexical results must still come back"


def test_fusion_is_weighted_towards_the_vector_ranking():
    """The measured trade: 0.8 vector weight kept exact-phrase retrieval at R@5 1.00 while a 0.5
    weight buried the semantic signal. The constant is part of the contract, not a taste."""
    assert embed_mod.RRF_VECTOR_WEIGHT == 0.8
    assert 0.0 < embed_mod.RRF_VECTOR_WEIGHT < 1.0
    # a document the vector ranking loves and the lexical one ignores still lands above a document
    # both rank last
    fused = embed_mod.fuse(["a", "b", "c"], ["d", "e"], k=60)
    assert fused.index("d") < fused.index("a"), fused
    # and when both rankings agree, they agree
    assert embed_mod.fuse(["a", "b"], ["a", "b"]) == ["a", "b"]


def test_vector_ranking_is_cosine_not_magnitude(vault):
    """A long entry must not win by being long: the ranking normalises."""
    near = embed_mod.unit([1.0, 0.0, 0.0])
    long_near = [x * 50 for x in near]
    far = embed_mod.unit([0.0, 1.0, 0.0])
    ranked = embed_mod.vector_ranking([1.0, 0.0, 0.0],
                                      {"short": near, "long": long_near, "far": far}, limit=3)
    assert ranked[:2] == ["short", "long"], ranked
    assert ranked[-1] == "far"


def test_entry_text_is_summary_ask_and_truncated_body():
    text = embed_mod.entry_text("summary here", "the question?", "x" * 5000)
    assert text.startswith("summary here")
    assert "the question?" in text
    assert len(text) < 1200, len(text)


def test_unpack_survives_a_json_round_trip():
    vec = [0.1, -0.25, 3.0]
    assert embed_mod.unpack(embed_mod.pack(vec)) == vec
    assert all(isinstance(x, float) for x in embed_mod.unpack(embed_mod.pack(vec)))
