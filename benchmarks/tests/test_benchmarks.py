"""Tests for the benchmark harness itself.

A harness that cannot be trusted to score correctly is worse than no harness: it publishes
numbers. These tests pin the three things that make a number mean something - the corpus is
deterministic, the metrics are what they claim, and a missing dataset produces no number at all.

They live under benchmarks/ on purpose: the shipped test suite (tests/) stays untouched.
Run them with:

    .venv/bin/python -m pytest benchmarks/tests -q
"""
from benchmarks.loaders import (
    BenchQuery,
    Dataset,
    corpus_sha256,
    load_longmemeval,
    load_synthetic,
    query_set_sha256,
)
from benchmarks.run import run
from sigil.store import Entry


def test_synthetic_corpus_is_deterministic():
    a = load_synthetic()
    b = load_synthetic()
    assert corpus_sha256(a.entries) == corpus_sha256(b.entries)
    assert query_set_sha256(a.queries) == query_set_sha256(b.queries)
    assert len(a.entries) == 200
    assert len(a.queries) == 100


def test_every_query_points_at_a_note_that_exists():
    ds = load_synthetic()
    ids = {e.id for e in ds.entries}
    for q in ds.queries:
        assert q.gold, f"{q.qid} has no gold entry"
        assert set(q.gold) <= ids, f"{q.qid} points at a missing entry"


def test_query_kinds_are_all_present():
    ds = load_synthetic()
    kinds = {q.kind for q in ds.queries}
    assert kinds == {"rare", "phrase", "paraphrase", "question"}


def test_metrics_count_what_they_say():
    from benchmarks.run import _metrics

    qs = [
        BenchQuery("a", "x", ("g1",), "rare"),
        BenchQuery("b", "y", ("g2",), "rare"),
        BenchQuery("c", "z", ("g3",), "rare"),
    ]
    ranked = {"a": ["g1", "other"], "b": ["other", "g2"], "c": ["other"]}
    m = _metrics(qs, ranked, [5, 10])
    assert m["recall_at_5"] == round(2 / 3, 4)
    assert m["mrr"] == round((1.0 + 0.5 + 0.0) / 3, 4)


def test_missing_dataset_yields_no_number():
    assert load_longmemeval("/nonexistent/longmemeval.json") is None


def test_run_writes_the_fields_a_result_must_carry(tmp_path):
    ds = Dataset(
        name="tiny",
        version="v0",
        entries=[
            Entry(id="e1", kind="fact", summary="kestrel write path for atlas", body="kestrel atlas"),
            Entry(id="e2", kind="fact", summary="anvil cold start for bramble", body="anvil bramble"),
        ],
        queries=[BenchQuery("q1", "kestrel atlas", ("e1",), "rare")],
    )
    result, _ranked = run(ds, [5])
    for field in ("dataset", "run", "k", "query_set_sha256", "corpus_sha256", "metrics", "misses"):
        assert field in result, f"result file is missing {field}"
    assert result["dataset"]["size"] == 2
    assert result["metrics"]["recall_at_5"] == 1.0
    assert result["run"]["git_rev"] != ""
    assert result["misses"] == []
