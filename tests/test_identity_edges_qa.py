"""Adversarial QA on entity identity, edge idempotency and scalar round-trip.

QA scope: tests/ only — quipu/ is untouched. Every test is deterministic: explicit `at=`
timestamps wherever time matters, fresh tmp vault per test. Assertions pin ACTUAL behaviour;
where that is outright wrong (contradicts docs/DESIGN.md §5 or silently loses data) the test is
xfailed STRICTLY with the defect reason, so it flips to a hard failure the moment quipu/ is fixed.
Finding numbers refer to the QA report that shipped with this file.
"""

import json

import pytest

from sigil import graph as graph_mod
from sigil.index import connect
from sigil.store import Entry, read_entry, write_entry

T1 = "2026-01-01T00:00:00Z"
T2 = "2026-06-01T00:00:00Z"
T3 = "2026-09-01T00:00:00Z"
MID = "2026-06-01T00:00:30Z"  # 30 s after T2: after the contradiction, before the re-assert


def _entities(cfg):
    """All entity rows as dicts (name, aliases json, key)."""
    c = connect(cfg.db_path)
    try:
        return [dict(r) for r in c.execute("SELECT name, aliases, key FROM entities").fetchall()]
    finally:
        c.close()


# --- 1. resolve() for a never-stored name -------------------------------------------


def test_resolve_of_unstored_name_returns_slug_and_lands_on_same_node(cfg):
    assert len(_entities(cfg)) == 0
    before = graph_mod.resolve(cfg, "Brand New Widget")
    assert before == "brand-new-widget", "an unstored name resolves to its own slug"

    eid = graph_mod.add_edge(cfg, "Brand New Widget", "part_of", "pip", at=T1)
    rows = graph_mod.timeline(cfg, "Brand New Widget")
    assert [r["src"] for r in rows] == [before], "add_edge landed on the node resolve predicted"
    assert rows[0]["id"] == eid
    assert graph_mod.resolve(cfg, "brand new widget") == before, "stored node resolves via its key"


# --- 2. alias accumulation -----------------------------------------------------------


def test_three_spellings_are_one_entity_row_with_aliases(cfg):
    graph_mod.add_edge(cfg, "Next.js", "depends_on", "react", at=T1)
    graph_mod.add_edge(cfg, "nextjs", "used_by", "vercel", at=T2)
    graph_mod.add_edge(cfg, "NEXT JS", "part_of", "web-stack", at=T3)

    ents = [e for e in _entities(cfg) if e["key"] == "nextjs"]
    assert len(ents) == 1, "three spellings must produce ONE row"
    assert set(json.loads(ents[0]["aliases"])) == {"Next.js", "nextjs", "NEXT JS"}
    # Display name is slug() of the first spelling inserted; the raw first spelling is kept as an
    # alias, so no information is lost (docs/DESIGN.md §7.4).
    assert ents[0]["name"] == "next-js"


def test_display_name_is_the_first_spelling_inserted(cfg):
    graph_mod.add_edge(cfg, "nextjs", "used_by", "vercel", at=T1)
    graph_mod.add_edge(cfg, "Next.js", "depends_on", "react", at=T2)
    graph_mod.add_edge(cfg, "NEXT JS", "part_of", "web-stack", at=T3)

    ents = [e for e in _entities(cfg) if e["key"] == "nextjs"]
    assert ents[0]["name"] == "nextjs", "insertion order, not preference, decides the display name"
    assert set(json.loads(ents[0]["aliases"])) == {"Next.js", "NEXT JS"}


# --- 3. unicode folding ----------------------------------------------------------------


def test_accented_cased_spellings_are_one_node(cfg):
    for name, pred, dst, ts in (
        ("Café", "part_of", "france", T1),
        ("cafe", "used_by", "bistro", T2),
        ("CAFE", "blocks", "tea-house", T3),
    ):
        graph_mod.add_edge(cfg, name, pred, dst, at=ts)

    assert graph_mod.identity("Café") == graph_mod.identity("cafe") == graph_mod.identity("CAFE")
    rows = graph_mod.timeline(cfg, "cafe")
    assert len(rows) == 3, "all three spellings land on one node"
    assert {r["src"] for r in rows} == {"caf"}
    ents = [e for e in _entities(cfg) if e["key"] == "cafe"]
    assert len(ents) == 1
    assert set(json.loads(ents[0]["aliases"])) == {"Café", "cafe", "CAFE"}
    # PINNED current behaviour (finding 3): slug() strips non-ASCII letters instead of folding
    # them, so the display name loses the accented character ("Café" -> "caf"). identity folds
    # correctly; the display-side mangle is the defect.
    assert ents[0]["name"] == "caf"


def test_non_latin_name_produces_a_stable_nonempty_identity_key(cfg):
    key_a = graph_mod.identity("日本語")
    assert key_a == graph_mod.identity("日本語"), "identity must be deterministic across calls"
    assert key_a, "non-empty even for a name with no ASCII alnum (actual fallback value: 'unknown')"

    eid = graph_mod.add_edge(cfg, "日本語", "part_of", "nihongo-doc", at=T1)
    rows = graph_mod.timeline(cfg, "日本語")
    assert len(rows) == 1 and rows[0]["id"] == eid, "add_edge+timeline round-trip works in isolation"


def test_distinct_non_latin_entities_stay_separate(cfg):
    graph_mod.add_edge(cfg, "日本語", "part_of", "nihongo-doc", at=T1)
    graph_mod.add_edge(cfg, "中文", "part_of", "zhongwen-doc", at=T1)

    # Both ends of an edge are entities (test_graph asserts materialize()==2 for ONE edge), so two
    # edges naming four distinct things is four rows. The claim under test is that the two non-Latin
    # names are separate nodes — not that the table holds exactly two rows.
    keys = [e["key"] for e in _entities(cfg)]
    assert {"日本語", "中文"} <= set(keys), "both non-Latin names must be entities"
    assert len(keys) == len(set(keys)), "no two entities may share a key"
    assert len(graph_mod.timeline(cfg, "日本語")) == 1
    assert len(graph_mod.timeline(cfg, "中文")) == 1


# --- 4. digits ---------------------------------------------------------------------------


def test_digit_only_entity_name_survives_and_round_trips(cfg):
    eid = graph_mod.add_edge(cfg, "04159069", "supersedes", "legacy-note", at=T1)

    ents = [e for e in _entities(cfg) if e["key"] == "04159069"]
    assert len(ents) == 1
    assert ents[0]["name"] == "04159069", "the digit-only name survives as a string, zero intact"
    assert all(isinstance(ents[0][f], str) for f in ("name", "key"))

    assert graph_mod.resolve(cfg, "04159069") == "04159069"
    rows = graph_mod.timeline(cfg, "04159069", at=T1)
    assert [r["src"] for r in rows] == ["04159069"] and rows[0]["id"] == eid

    # The graph treats a leading zero as identity-relevant (unlike the markdown store, which must
    # quote digit-only strings to survive parsing): "04159069" and "4159069" stay separate nodes.
    assert graph_mod.identity("04159069") != graph_mod.identity("4159069")


# --- 5. contradiction history (docs/DESIGN.md §5) ----------------------------------------


def test_contradiction_history_three_windows_and_point_in_time(cfg):
    first = graph_mod.add_edge(cfg, "svc", "depends_on", "redis", at=T1)
    second = graph_mod.contradict(cfg, first, "svc", "depends_on", "postgres", at=T2)
    third = graph_mod.add_edge(cfg, "svc", "depends_on", "redis", at=T3)

    # DESIGN.md §5 — "a re-opened window is a new edge, not a revival": CONFIRMED. Re-asserting
    # redis at T3 produced a THIRD row with a fresh window; the original row stays closed at T2
    # and is never un-closed.
    rows = graph_mod.timeline(cfg, "svc")
    assert len(rows) == 3, "three assertions, three windows"
    assert len({first, second, third}) == 3
    closed = next(r for r in rows if r["id"] == first)
    assert closed["valid_to"] == T2 and closed["dst"] == "redis"
    reopened = next(r for r in rows if r["id"] == third)
    assert reopened["valid_from"] == T3 and reopened["valid_to"] is None

    # At T2+30 s only the replacement is believed: the old window ended at T2, the re-opened one
    # starts at T3.
    mid = graph_mod.timeline(cfg, "svc", at=MID)
    assert [r["dst"] for r in mid] == ["postgres"]


def test_failed_replacement_gives_back_a_live_belief(cfg):
    oid = graph_mod.add_edge(cfg, "svc", "depends_on", "redis", at=T1)
    with pytest.raises(ValueError):
        graph_mod.contradict(cfg, oid, "svc", "not_in_any_vocab", "postgres", at=T2)

    rows = graph_mod.timeline(cfg, "svc")
    live = [r for r in rows if r["valid_to"] is None]
    assert len(live) == 1, "the old belief must stay live when the replacement is rejected"
    assert live[0]["id"] == oid and live[0]["dst"] == "redis"


# --- 6. idempotency edge cases -----------------------------------------------------------


def test_same_triple_with_a_different_note_id_returns_the_existing_edge(cfg):
    # record=False: this test is about the ROW's provenance, and #94 made a recorded carrier one
    # that must resolve to an entry. Whether the markdown records the spec is test_edge_carrier.py's
    # subject; `note-1` here is a provenance label, not a note this fixture creates.
    first = graph_mod.add_edge(cfg, "app", "depends_on", "db", note_id="note-1", at=T1, record=False)
    same_second = graph_mod.add_edge(cfg, "APP", "depends_on", "DB", note_id="note-2", at=T1,
                                     record=False)
    later = graph_mod.add_edge(cfg, "app", "depends_on", "db", note_id="note-3", at=T2, record=False)

    assert first == same_second == later, "the existing edge id is returned whatever the note_id"
    rows = graph_mod.timeline(cfg, "app")
    assert len(rows) == 1
    # CURRENT CONTRACT (documented, not a bug): a re-assertion never rewrites note_id — the first
    # assertion's provenance wins. Callers who want the new note must invalidate + re-assert.
    assert rows[0]["note_id"] == "note-1"


def test_reasserting_after_invalidation_creates_a_new_edge(cfg):
    # record=False: provenance labels, not entries — see the note on the test above (#94)
    old = graph_mod.add_edge(cfg, "svc", "depends_on", "cache-v1", note_id="n1", at=T1, record=False)
    graph_mod.invalidate(cfg, old, at=T2)
    new = graph_mod.add_edge(cfg, "svc", "depends_on", "cache-v1", note_id="n2", at=T3, record=False)

    assert new != old, "invalidation is not deletion — a re-assert opens a fresh window"
    rows = graph_mod.timeline(cfg, "svc")
    assert len(rows) == 2, "history is preserved"
    closed = next(r for r in rows if r["id"] == old)
    assert closed["valid_to"] == T2 and closed["note_id"] == "n1"
    live = next(r for r in rows if r["id"] == new)
    assert live["valid_from"] == T3 and live["valid_to"] is None and live["note_id"] == "n2"


# --- 7. timeline boundary semantics ------------------------------------------------------


def test_timeline_lower_bound_is_inclusive(cfg):
    eid = graph_mod.add_edge(cfg, "svc", "depends_on", "db", at=T1)
    graph_mod.invalidate(cfg, eid, at=T2)
    # Contract: the validity window is half-open [valid_from, valid_to) — the start instant is IN.
    assert len(graph_mod.timeline(cfg, "svc", at=T1)) == 1
    assert len(graph_mod.timeline(cfg, "svc", at="2025-12-31T23:59:59Z")) == 0


def test_timeline_upper_bound_is_exclusive(cfg):
    eid = graph_mod.add_edge(cfg, "svc", "depends_on", "db", at=T1)
    graph_mod.invalidate(cfg, eid, at=T2)
    # Contract: at the exact valid_to instant the belief is already gone.
    assert len(graph_mod.timeline(cfg, "svc", at=T2)) == 0
    assert len(graph_mod.timeline(cfg, "svc", at="2026-05-31T23:59:59Z")) == 1


def test_timeline_compares_timestamps_lexicographically(cfg):
    eid = graph_mod.add_edge(cfg, "svc", "depends_on", "db", at=T1)
    graph_mod.invalidate(cfg, eid, at=T2)
    # PINNED current contract: valid_from/valid_to/`at` compare as TEXT, so `at` must carry the
    # same fixed-width "%Y-%m-%dT%H:%M:%SZ" shape ("Z" sorts after "." — a millisecond `at`
    # silently misses a window opened at the same instant).
    assert len(graph_mod.timeline(cfg, "svc", at="2026-01-01T00:00:00.500Z")) == 0
    assert len(graph_mod.timeline(cfg, "svc", at="2026-03-01T00:00:00Z")) == 1


# --- scalar round-trip (markdown store) --------------------------------------------------


def test_negative_numeric_strings_stay_strings(cfg):
    e = Entry(kind="fact", source="-42", project="-000123", summary="negative literals")
    back = read_entry(write_entry(cfg.root, e))
    assert back.source == "-42" and isinstance(back.source, str)
    assert back.project == "-000123", "leading-zero negatives must survive as strings"


def test_exotic_numeric_looking_strings_round_trip(cfg):
    e = Entry(kind="fact", project="1.5e3", agent="1_0", summary="exponent and underscore literals")
    back = read_entry(write_entry(cfg.root, e))
    assert back.project == "1.5e3", "1.5e3 must survive as the string it was written as"
    assert back.agent == "1_0", "1_0 must survive as the string it was written as"