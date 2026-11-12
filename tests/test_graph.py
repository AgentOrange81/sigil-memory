import pytest

from sigil import graph as graph_mod


def test_edge_requires_known_predicate(cfg):
    with pytest.raises(ValueError):
        graph_mod.add_edge(cfg, "trx40", "vibes_with", "docp")
    eid = graph_mod.add_edge(cfg, "trx40", "measured_as", "docp-3200")
    assert isinstance(eid, int)


def test_allow_new_predicate_escape_hatch(cfg):
    graph_mod.add_edge(cfg, "a", "custom_pred", "b", allow_new_predicate=True)
    assert graph_mod.timeline(cfg, "a")[0]["predicate"] == "custom_pred"


def test_invalidate_closes_window_but_keeps_history(cfg):
    eid = graph_mod.add_edge(cfg, "trx40", "measured_as", "docp-3600", at="2026-01-01T00:00:00Z")
    graph_mod.invalidate(cfg, eid, at="2026-06-01T00:00:00Z")
    rows = graph_mod.timeline(cfg, "trx40")
    assert len(rows) == 1, "history must survive invalidation"
    assert rows[0]["valid_to"] == "2026-06-01T00:00:00Z"
    assert rows[0]["valid_from"] == "2026-01-01T00:00:00Z"


def test_timeline_as_of_a_date(cfg):
    eid = graph_mod.add_edge(cfg, "trx40", "measured_as", "docp-3600", at="2026-01-01T00:00:00Z")
    graph_mod.invalidate(cfg, eid, at="2026-06-01T00:00:00Z")
    assert len(graph_mod.timeline(cfg, "trx40", at="2026-03-01T00:00:00Z")) == 1
    assert len(graph_mod.timeline(cfg, "trx40", at="2026-09-01T00:00:00Z")) == 0


def test_contradict_closes_old_and_opens_new(cfg):
    old = graph_mod.add_edge(cfg, "trx40", "measured_as", "docp-3600", at="2026-01-01T00:00:00Z")
    new = graph_mod.contradict(cfg, old, "trx40", "measured_as", "docp-3200", at="2026-06-01T00:00:00Z")
    assert new != old
    rows = graph_mod.timeline(cfg, "trx40")
    live = [r for r in rows if r["valid_to"] is None]
    assert [r["dst"] for r in live] == ["docp-3200"]
    assert len(rows) == 2


def test_invalidating_a_closed_edge_fails(cfg):
    eid = graph_mod.add_edge(cfg, "a", "blocks", "b")
    graph_mod.invalidate(cfg, eid)
    with pytest.raises(ValueError):
        graph_mod.invalidate(cfg, eid)


def test_neighbors_walks_two_hops(cfg):
    graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite")
    graph_mod.add_edge(cfg, "sqlite", "used_by", "fts5")
    one = graph_mod.neighbors(cfg, "quipu", depth=1)
    two = graph_mod.neighbors(cfg, "quipu", depth=2)
    assert {n["name"] for n in one["nodes"]} == {"quipu", "sqlite"}
    assert "fts5" in {n["name"] for n in two["nodes"]}


def test_punctuation_variants_are_one_entity(cfg):
    """Regression: slug() kept separators, so "Next.js" (next-js) and "nextjs" (nextjs) were two nodes."""
    graph_mod.add_edge(cfg, "Next.js", "depends_on", "react")
    graph_mod.add_edge(cfg, "nextjs", "used_by", "vercel")       # same identity, other spelling
    graph_mod.add_edge(cfg, "Next JS", "part_of", "web-stack")   # and again
    rows = graph_mod.timeline(cfg, "nextjs")
    assert len(rows) == 3, "all three assertions belong to one node"
    assert {r["src"] for r in rows} == {"next-js"}, "the first display spelling wins"


def test_duplicate_assertion_is_idempotent(cfg):
    a = graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite")
    b = graph_mod.add_edge(cfg, "Quipu", "depends_on", "SQLite")   # same instant, same identity
    assert a == b
    assert len(graph_mod.timeline(cfg, "quipu")) == 1


def test_reasserting_later_does_not_duplicate_the_live_edge(cfg):
    """CI caught this: two calls a second apart made two live edges for one fact."""
    a = graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite", at="2026-01-01T00:00:00Z")
    b = graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite", at="2026-01-02T00:00:00Z")
    assert a == b
    live = [r for r in graph_mod.timeline(cfg, "quipu") if r["valid_to"] is None]
    assert len(live) == 1, "a re-asserted fact must not open a second window"


def test_identity_ignores_separators_and_accents(cfg):
    graph_mod.add_edge(cfg, "FTS-5", "used_by", "sqlite")
    graph_mod.add_edge(cfg, "fts5", "part_of", "sqlite")
    graph_mod.add_edge(cfg, "Café", "part_of", "quipu")
    graph_mod.add_edge(cfg, "cafe", "blocks", "quipu")
    assert len(graph_mod.timeline(cfg, "fts5")) == 2
    assert len(graph_mod.timeline(cfg, "cafe")) == 2


def test_version_qualifier_is_a_different_entity(cfg):
    """A version is a property of the assertion, not another spelling of the entity."""
    graph_mod.add_edge(cfg, "Next.js", "depends_on", "react")
    graph_mod.add_edge(cfg, "Next.js 16", "depends_on", "react")
    assert len(graph_mod.timeline(cfg, "Next.js")) == 1
    assert len(graph_mod.timeline(cfg, "Next.js 16")) == 1


def test_aliases_are_recorded_for_every_spelling(cfg):
    from sigil.index import connect
    graph_mod.add_edge(cfg, "Next.js", "depends_on", "react")
    graph_mod.add_edge(cfg, "NEXTJS", "used_by", "vercel")
    c = connect(cfg.db_path)
    row = c.execute("SELECT name, aliases, key FROM entities WHERE key = 'nextjs'").fetchone()
    c.close()
    assert row[0] == "next-js" and row[2] == "nextjs"
    assert "NEXTJS" in row[1] and "Next.js" in row[1]


def test_materialize_writes_entity_hubs(cfg):
    graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite")
    assert graph_mod.materialize(cfg) == 2
    hub = (cfg.entities_dir / "quipu.md").read_text()  # an entity called quipu, not the package
    assert "-->" in hub and "[[sqlite]]" in hub
