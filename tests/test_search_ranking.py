"""Retrieval ranks where the match lands, and a thin AND result is topped up (#31, #43).

Two defects, one code path:

#31  bm25 scores a match wherever it lands, so a note whose BODY contains the query words tied with —
     and on a short body beat — the note whose SUMMARY is the claim. The fixture in the first test is
     taken from a measured case: with unweighted columns the body note wins (-9.34 against -8.43),
     with the shipped weights the summary note wins by 6.6 (-13.09 against -6.51).

#43  Every token is ANDed and the OR fallback only ran on an EMPTY result, so one rare token in one
     unrelated note produced a one-row answer that looked complete.

The tests assert ORDER and COMPLETENESS, not exact scores: bm25 magnitudes depend on corpus size, and
a test that pins them breaks the moment the corpus fixture grows. The corpus is padded to 32 notes so
term frequency is meaningful — in a three-note vault every score rounds to zero and the ranking tells
you nothing.
"""

from sigil import index as index_mod
from sigil.store import Entry, write_entry

FILLER = 30


def _pad(cfg, n=FILLER, project="wraith"):
    """Unrelated notes so idf and length normalisation are not degenerate."""
    for i in range(n):
        write_entry(cfg.root, Entry(kind="fact", state="promoted", project=project,
                                    summary=f"unrelated note {i}",
                                    body="docker volumes and disk layout, nothing else"))


def _indexed(cfg):
    index_mod.reindex(cfg.root, cfg.db_path)
    return cfg.db_path


def test_a_summary_match_outranks_a_body_match(cfg):
    """The claim you can read in the list outranks the transcript that merely mentions the words."""
    body_note = Entry(kind="capture", state="raw", project="quipu", summary="n",
                      body="kestrel cache invalidation")
    summary_note = Entry(kind="decision", state="promoted", project="quipu",
                         summary="Kestrel cache invalidation belongs on the write path, not on read",
                         body="measured: two runs, no stale reads")
    write_entry(cfg.root, body_note)
    write_entry(cfg.root, summary_note)
    _pad(cfg)

    rows = index_mod.search(_indexed(cfg), "kestrel cache invalidation", limit=2)
    assert [r["id"] for r in rows] == [summary_note.id, body_note.id], \
        [(r["summary"], r["score"]) for r in rows]


def test_thin_and_result_is_topped_up_from_the_or_ranking(cfg):
    """A one-row AND answer is not an answer when the caller asked for five (#43)."""
    all_tokens = Entry(kind="decision", state="promoted", project="quipu",
                       summary="The kestrel feeds on cache invalidation storms",
                       body="one full sentence with every word in it")
    write_entry(cfg.root, all_tokens)
    partial = []
    for i in range(4):
        e = Entry(kind="fact", state="promoted", project="quipu",
                  summary=f"kestrel note {i}", body="a bird, not a database")
        partial.append(e)
        write_entry(cfg.root, e)
    _pad(cfg)

    rows = index_mod.search(_indexed(cfg), "kestrel cache invalidation", limit=5)
    ids = [r["id"] for r in rows]
    assert len(ids) == 5, f"the AND set is thin, the OR ranking has to fill it: {len(ids)} rows"
    assert len(set(ids)) == len(ids), f"a merged result must not repeat an entry: {ids}"
    assert ids[0] == all_tokens.id, "the all-token match still ranks first"
    assert set(ids[1:]) <= {e.id for e in partial}, ids


def test_a_full_and_result_is_not_topped_up(cfg):
    """Top-up is for a thin result. When the AND ranking already fills the ask, nothing is added."""
    entries = [Entry(kind="decision", state="promoted", project="quipu",
                     summary=f"kestrel cache invalidation runbook {i}") for i in range(3)]
    for e in entries:
        write_entry(cfg.root, e)
    write_entry(cfg.root, Entry(kind="fact", state="promoted", project="quipu",
                                summary="kestrel only, no other words"))
    _pad(cfg)

    rows = index_mod.search(_indexed(cfg), "kestrel cache invalidation", limit=2)
    assert len(rows) == 2
    assert all("runbook" in r["summary"] for r in rows), \
        [r["summary"] for r in rows]


def test_the_or_top_up_respects_the_filters(cfg):
    """A top-up must not smuggle in an entry the caller filtered out — that turns a filter into a
    suggestion."""
    write_entry(cfg.root, Entry(kind="decision", state="promoted", project="quipu",
                                summary="The kestrel feeds on cache invalidation storms"))
    write_entry(cfg.root, Entry(kind="fact", state="promoted", project="goonify",
                                summary="kestrel notes from a different project"))
    _pad(cfg)

    rows = index_mod.search(_indexed(cfg), "kestrel cache invalidation", project="quipu", limit=5)
    assert rows, "the AND hit is in the requested project"
    assert all(r["project"] == "quipu" for r in rows), [(r["summary"], r["project"]) for r in rows]


def test_search_on_an_empty_index_returns_nothing(cfg):
    assert index_mod.search(_indexed(cfg), "kestrel") == []


def test_weights_are_per_column_not_per_entry(cfg):
    """The weight applies to the column the match landed in, in schema order. A mismatch between the
    dict and the bm25 argument order would silently rank by body — this pins the order."""
    assert list(index_mod.COLUMN_WEIGHTS) == ["summary", "ask", "evidence", "body"]
    weights = [index_mod.COLUMN_WEIGHTS[c] for c in index_mod.COLUMN_WEIGHTS]
    assert weights == sorted(weights, reverse=True), \
        "the retrieval handle must weigh most: summary > ask > evidence > body"
    assert weights[0] / weights[-1] >= 4, f"the ranking is barely weighted: {weights}"
