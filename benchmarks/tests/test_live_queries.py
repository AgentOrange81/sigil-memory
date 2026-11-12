"""Tests for the live-vault instrument.

The whole point of #47 is that one column said 9/10 while the honest answer was 6/10, so the instrument
that reports those numbers needs its own gates:

1. the weights the "shipped" row uses are READ from the package, not retyped (a stale copy once labelled
   10/6/3/1 as "shipped weights" while the code shipped 10/4/2/0.5);
2. the gold column cannot be satisfied by a subject mention — that is the defect, encoded;
3. an empty gold list means "nothing in the corpus answers this", and it scores as a miss for gold and
   for gold_in_5 rather than being skipped;
4. the local copy of search() agrees with the shipped function row for row on the same corpus, because a
   variant that disagrees at the baseline is measuring itself.
"""
from __future__ import annotations

import pytest

from benchmarks import live_queries as lq
from sigil.index import COLUMN_WEIGHTS, reindex
from sigil.store import Entry, write_entry


def _hit(eid, summary, state="promoted"):
    return {"id": eid, "summary": summary, "state": state, "kind": "fact", "score": -1.0}


def test_shipped_weights_are_read_from_the_package():
    assert lq.SHIPPED == tuple(COLUMN_WEIGHTS[c] for c in lq.COLUMNS)
    assert lq.COLUMNS == ("summary", "ask", "evidence", "body")


def test_gold_column_is_not_satisfied_by_a_subject_mention(monkeypatch):
    monkeypatch.setattr(lq, "QUERIES", [
        ("what did the gateway default to", ("gateway",), ("answer-id",)),
        ("which gpu should i buy", ("gpu",), ()),
    ])

    def hits_for(question):
        if question.startswith("what did"):
            return [_hit("audit-blob", "hardware audit that mentions the gateway once")]
        return [_hit("unrelated", "solana testing notes")]

    report = lq.score(hits_for)
    # both questions "mention the subject" — and neither answers its question
    assert report["rank1_subject"] == 1
    assert report["rank1_gold"] == 0
    assert report["gold_in_5"] == 0
    assert report["rows"][0]["rank1_subject"] is True
    assert report["rows"][0]["rank1_gold"] is False


def test_an_empty_gold_list_scores_as_a_miss_not_a_skip(monkeypatch):
    monkeypatch.setattr(lq, "QUERIES", [("which gpu should i buy", ("gpu",), ())])

    report = lq.score(lambda q: [_hit("x", "gpu comparison notes")])
    assert report["n"] == 1
    assert report["rank1_subject"] == 1
    assert report["rank1_gold"] == 0
    assert report["gold_in_5"] == 0
    assert report["rows"][0]["gold_ids"] == []


def test_gold_in_five_credits_a_right_answer_that_is_not_rank_one(monkeypatch):
    monkeypatch.setattr(lq, "QUERIES", [("how do we deploy", ("deploy",), ("gold-id",))])

    report = lq.score(lambda q: [_hit("noise", "deploy pipeline chatter"), _hit("gold-id", "the recipe")])
    assert report["rank1_gold"] == 0
    assert report["gold_in_5"] == 1


def test_the_shipped_row_calls_the_shipped_function(tmp_path, monkeypatch):
    seen = []

    def fake_search(db, query, **kw):
        seen.append((str(db), query, kw))
        return []

    monkeypatch.setattr(lq, "search", fake_search)
    lq.shipped_row("db.sqlite", "probe")
    assert seen and seen[0][1] == "probe" and seen[0][2]["limit"] == 5


def test_the_variant_copy_agrees_with_the_shipped_function(tmp_path):
    """A variant row is only evidence if its baseline reproduces the shipped ranking."""
    root = tmp_path / "vault"
    root.mkdir()
    notes = [
        ("kestrel write path for atlas", "kestrel atlas pipeline"),
        ("anvil cold start for bramble", "anvil bramble boot"),
        ("gateway default model for the fleet", "gateway model fleet"),
        ("unrelated note about paint", "paint drying"),
    ]
    for i, (summary, body) in enumerate(notes):
        write_entry(root, Entry(id=f"e{i}", kind="fact", summary=summary, body=body))
    db = tmp_path / "index.db"
    reindex(root, db)

    for query in ("kestrel atlas", "gateway model fleet", "paint"):
        shipped = [r["id"] for r in lq.shipped_row(db, query)]
        copy = [r["id"] for r in lq.variant_search(db, query, weights=lq.SHIPPED, mode="and_thin")]
        assert copy == shipped, f"{query!r}: copy {copy} != shipped {shipped}"


@pytest.mark.parametrize("question,stems,gold", lq.QUERIES)
def test_query_rows_are_well_formed(question, stems, gold):
    assert isinstance(question, str) and question and "?" not in question, \
        f"{question!r} should be the plain question text, without punctuation"
    assert stems, f"{question!r} has no subject stems"
    assert len(set(gold)) == len(gold), "duplicate gold id"
    for eid in gold:
        assert isinstance(eid, str) and 1 <= len(eid) <= 12 and " " not in eid


def test_every_empty_gold_list_explains_itself():
    for question, _stems, gold in lq.QUERIES:
        if not gold:
            assert lq.NOTES.get(question), f"{question!r} has no gold and no note saying why"


def test_ten_questions_and_no_duplicates():
    questions = [q for q, _s, _g in lq.QUERIES]
    assert len(questions) == 10
    assert len(set(questions)) == len(questions)
