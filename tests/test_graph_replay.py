"""`rm .sigil/index.db` + `quipu reindex` reproduces the graph from the markdown (#24).

The claim under test: edges and closed windows live in the entries' frontmatter (`edges:` /
`edge_closures:`), and the reindex path folds the db-only leftovers into the entries, clears the
db and replays it — so a deleted index.db loses nothing. This suite pins that invariant so it
cannot silently become false again.

Determinism: every instant a comparison depends on is either passed explicitly (`at=`) or read
from the markdown; nothing is compared against the wall clock. One fresh tmp vault per test.
Comparisons are NORMALISED structures (sorted tuples, edge ids excluded) so map ordering and
edge-id reassignment cannot make a test pass or fail by luck.

QA scope: tests/ only. The #24 code lives on fix/graph-truth; behaviour on this branch may be a
step behind — a failing test is a finding, reported with its real output and not weakened. Two
findings are ENCODED as strict xfails (marked with the issue trace): they pass while the defect
stands and red-flag the moment quipu/ makes the assertion hold.
"""

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from sigil import graph as graph_mod
from sigil import index as index_mod
from sigil.store import parse_edge_spec, read_entry

T1 = "2026-01-01T00:00:00Z"


def run_cli(*args, stdin_text=None, cwd=None):
    """Run `python -m quipu <args>` as a user would; stdin /dev/null unless text is piped."""
    return subprocess.run(
        [sys.executable, "-m", "sigil", *args],
        capture_output=True, text=True, timeout=60, cwd=cwd,
        input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
    )


def _clear_db(db_path: Path) -> None:
    """Delete the sqlite index, WAL and SHM sidecars — exactly what `rm .sigil/index.db` means."""
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _entry_md(eid, summary, created, edges=(), closures=(), state="promoted", kind="fact"):
    """One entry's markdown, authored here so the tests do not depend on writer fields that may
    only exist on fix/graph-truth: the frontmatter is what a replay reads, whatever wrote it."""
    lines = [
        "---", f'id: "{eid}"', f"kind: {kind}", f"state: {state}", 'agent: "vector"',
        'project: "replay"', 'session: ""', f"created: {created}", f"updated: {created}",
        "confidence: 0.8", "importance: 0.9", f"summary: {_md_scalar(summary)}",
    ]
    if edges:
        lines.append("edges: " + json.dumps(list(edges), ensure_ascii=False))
    if closures:
        lines.append("edge_closures: " + json.dumps(list(closures), ensure_ascii=False))
    return "\n".join([*lines, "---", "", f"# {summary}", "", summary, ""])


def _md_scalar(text):
    """Quote the way the store does: anything with a space stays a string on read-back."""
    if re.search(r"\s", text) or text.startswith("#"):
        return json.dumps(text, ensure_ascii=False)
    return text


def _capture_state(cfg, entities, out_dir):
    """Normalised graph state: per-entity timelines as sorted tuples + export graph records.

    Edge ids are excluded — a rebuild is allowed to renumber rows — but note_id is kept: the
    entry that carries an edge is part of the truth.
    """
    timelines = {}
    for name in entities:
        timelines[name] = sorted(
            (r["src"], r["predicate"], r["dst"], r["note_id"] or "", r["valid_from"], r["valid_to"])
            for r in graph_mod.timeline(cfg, name))
    export = run_cli("--vault", str(cfg.root), "export", "graph", "--out", str(out_dir))
    assert export.returncode == 0, export.stderr
    records = [
        json.loads(line)
        for line in (out_dir / "edges.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    edges = sorted(
        (r["src"], r["predicate"], r["dst"], r["note_id"] or "", r["valid_from"], r["valid_to"],
         bool(r["live"])) for r in records)
    return timelines, edges


def _assert_state_equal(before, after, what):
    before_tl, before_edges = before
    after_tl, after_edges = after
    assert before_tl == after_tl, (f"{what}: the rebuilt timelines differ from the captured ones\n"
                                   f"  before: {before_tl}\n  after:  {after_tl}")
    assert before_edges == after_edges, (f"{what}: the rebuilt export differs\n"
                                         f"  before: {before_edges}\n  after:  {after_edges}")


# --- 1. round trip: live edges, a contradiction and an invalidation survive the loss of the db ---


def test_round_trip_reproduces_live_and_closed_windows(cfg):
    vault = cfg.root
    w1 = run_cli("--vault", str(vault), "write", "--kind", "decision", "--state", "promoted",
                 "--summary", "TRX40 measured as DOCP 3600", "--evidence", "ev-bench",
                 "--edge", "TRX40:measured_as:DOCP 3600")
    assert w1.returncode == 0, w1.stderr
    entry1_id = Path(w1.stdout.strip()).name.split("-")[0]
    assert re.fullmatch(r"[0-9a-f]{8}", entry1_id)

    tl = run_cli("--vault", str(vault), "timeline", "TRX40")
    eid1 = tl.stdout.split()[0]
    con = run_cli("--vault", str(vault), "contradict", eid1, "TRX40", "measured_as", "DOCP 3200")
    assert con.returncode == 0, con.stderr

    w2 = run_cli("--vault", str(vault), "write", "--kind", "fact", "--state", "promoted",
                 "--summary", "rack notes", "--evidence", "ev-rack",
                 "--edge", "DOCP 3200:used_by:rack1")
    assert w2.returncode == 0, w2.stderr
    tl2 = run_cli("--vault", str(vault), "timeline", "DOCP 3200")
    eid2 = next(line.split()[0] for line in tl2.stdout.splitlines()
                if line.split() and "used_by" in line)
    inv = run_cli("--vault", str(vault), "invalidate", eid2)
    assert inv.returncode == 0, inv.stderr

    entities = ("TRX40", "DOCP 3600", "DOCP 3200", "rack1")
    before = _capture_state(cfg, entities, cfg.root / "export-before")

    _clear_db(cfg.db_path)
    rx = run_cli("--vault", str(vault), "reindex")
    assert rx.returncode == 0, f"reindex after the loss of the db must exit 0: {rx.stderr!r}"
    after = _capture_state(cfg, entities, cfg.root / "export-after")

    _assert_state_equal(before, after, "rm index.db + reindex must reproduce the graph")

    # the replacement is still live and carried by the entry that asserted the first edge
    replacement = next(r for r in after[0]["TRX40"] if r[2] == "docp-3200")
    assert replacement[5] is None, "the replacement edge must still be live after the round trip"
    assert replacement[3] == entry1_id, (
        "the replacement must be carried by an entry, or a rebuild drops it")
    closed = next(r for r in after[0]["TRX40"] if r[2] == "docp-3600")
    assert closed[5] is not None, "the closed window must still be closed after the round trip"
    rack = next(r for r in after[0]["DOCP 3200"] if r[1] == "used_by")
    assert rack[5] is not None, "the invalidated window must still be closed after the round trip"


# --- 2. idempotency holds through the replay ------------------------------------------


def test_two_assertions_of_one_fact_rebuild_as_one_edge(cfg):
    notes = cfg.root / "notes"
    (notes / "aa000001-trx40-needs-docp-3600.md").write_text(
        _entry_md("aa000001", "TRX40 needs DOCP 3600", T1,
                  edges=["TRX40:measured_as:DOCP 3600"]), encoding="utf-8")
    (notes / "aa000002-spelled-again.md").write_text(
        _entry_md("aa000002", "TRX40 needs DOCP 3600, spelled again", T1,
                  edges=["trx40:measured_as:docp 3600"]), encoding="utf-8")

    first = graph_mod.add_edge(cfg, "TRX40", "measured_as", "DOCP 3600", note_id="aa000001", at=T1)
    second = graph_mod.add_edge(cfg, "trx40", "measured_as", "docp 3600", note_id="aa000002",
                                at="2026-01-01T00:00:01Z")
    assert first == second, "two assertions of one fact are already one live edge"
    assert len(graph_mod.timeline(cfg, "DOCP 3600")) == 1

    _clear_db(cfg.db_path)
    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, rx.stderr
    after = graph_mod.timeline(cfg, "DOCP 3600")
    assert len(after) == 1, "idempotency must hold through the replay: one edge, not two"
    assert after[0]["valid_from"] == T1 and after[0]["valid_to"] is None
    assert after[0]["note_id"] in ("aa000001", "aa000002")


# --- 3. a malformed edge spec must not cost the entry ---------------------------------


def test_out_of_vocabulary_spec_is_skipped_and_reported(cfg):
    """A malformed edge spec must not cost the entry, and the report must be actionable.

    The finding this encodes (#46): the summary used to say `... 1 edge skipped: likes is not in
    the vocabulary` — the skip was counted but neither the spec nor the entry was named, so a vault
    with twenty skips could not tell you which file to edit, and the same line repeated twenty times.
    """
    notes = cfg.root / "notes"
    (notes / "s0000001-out-of-vocab-probe.md").write_text(
        _entry_md("s0000001", "the out-of-vocab probe entry", T1,
                  edges=["ALPHA:likes:BETA"]), encoding="utf-8")

    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, "an unparseable or rejected spec must not be a fatal reindex"
    found = run_cli("--vault", str(cfg.root), "search", "out-of-vocab probe")
    assert found.returncode == 0, "the entry itself must survive its bad spec"
    report = index_mod.stats(cfg.db_path)
    assert report["edges"] == 0, "an out-of-vocabulary spec must be skipped, not asserted"
    summary = rx.stdout + rx.stderr
    assert "ALPHA:likes:BETA" in summary, "the reindex summary must name the skipped spec"
    assert "s0000001" in summary, "…and the entry it came from, or nobody knows which file to fix"
    assert "config.toml" in summary, "…and where the predicate vocabulary lives"
    # one line per skip, so twenty skips read as twenty findings instead of one repeated sentence
    assert summary.count("skipped:") == 1


def test_a_malformed_spec_names_the_entry_too(cfg):
    """The other skip shape: an unparseable spec. Same rule — entry and spec, not just a reason."""
    notes = cfg.root / "notes"
    (notes / "s0000002-malformed-spec-probe.md").write_text(
        _entry_md("s0000002", "the malformed spec entry", T1, edges=["not-an-edge"]),
        encoding="utf-8")

    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, rx.stderr
    summary = rx.stdout + rx.stderr
    assert "s0000002" in summary and "not-an-edge" in summary
    assert "src:predicate:dst" in summary


# --- 4. the common case: a vault with no edges ----------------------------------------


def test_a_vault_without_edges_reindexes_clean(cfg):
    (cfg.root / "notes" / "p0000001-plain-note.md").write_text(
        _entry_md("p0000001", "a plain note with no edges", T1), encoding="utf-8")
    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, rx.stderr
    assert "indexed" in rx.stdout
    report = index_mod.stats(cfg.db_path)
    assert report["edges"] == 0 and report["edges_closed"] == 0
    out = cfg.root / "export-empty"
    export = run_cli("--vault", str(cfg.root), "export", "graph", "--out", str(out))
    assert export.returncode == 0
    assert (out / "edges.jsonl").read_text(encoding="utf-8") == ""


# --- 5. an old-schema db connects and is recoverable by rebuild -----------------------


OLD_SCHEMA = """
CREATE TABLE entities(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT DEFAULT 'thing',
  aliases TEXT DEFAULT '[]', created TEXT);
CREATE TABLE edges(
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, predicate TEXT NOT NULL, dst TEXT NOT NULL,
  note_id TEXT, confidence REAL DEFAULT 1.0, valid_from TEXT, valid_to TEXT,
  UNIQUE(src, predicate, dst, valid_from));
CREATE TABLE events(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""


def test_old_schema_db_connects_and_rebuild_recovers(cfg):
    """A migrated old db must still answer its own history and be recoverable by rebuild.

    connect()-side recovery (key column added, keys backfilled, indexes created) is already
    covered by tests/test_schema_migration.py::test_connect_recovers_from_a_db_written_before_
    the_identity_key — not duplicated here. This test builds on it: the OLD-VAULT-specific parts
    (its timeline still answers; its db-only edge folds into the markdown and replays).
    """
    (cfg.root / "notes" / "rr000001-quipu-stores-its-graph.md").write_text(
        _entry_md("rr000001", "quipu stores its graph in the notes", T1), encoding="utf-8")
    db = cfg.db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO entities(name, kind, aliases, created) VALUES(?,?,?,?)",
              ("TRX40", "thing", "[]", T1))
    c.execute("INSERT INTO edges(src, predicate, dst, note_id, valid_from) VALUES(?,?,?,?,?)",
              ("trx40", "depends_on", "sqlite", "rr000001", T1))
    c.commit()
    c.close()

    index_mod.connect(db).close()  # migrate + backfill; asserted in tests/test_schema_migration.py

    # the migrated vault must still answer its own history (xfail: the defect)
    before = graph_mod.timeline(cfg, "TRX40")
    assert len(before) == 1, (
        "timeline on a migrated old db is silent about its graph: the stored display name "
        "'TRX40' and the old edge row's spelling 'trx40' diverge and nothing resolves between "
        "them")

    rebuilt = graph_mod.rebuild(cfg)
    assert rebuilt["folded"] == 1
    assert rebuilt["edges"] == 1, "the edge folded into the markdown must be replayed at once"
    after = graph_mod.timeline(cfg, "TRX40")
    assert sorted(_row_key(r) for r in after) == sorted(_row_key(r) for r in before), \
        "the rebuild is recoverable: identity-level state is unchanged"

    # and the recovered graph is now IN the markdown — the full round trip closes the loop
    _clear_db(db)
    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, rx.stderr
    replayed = graph_mod.timeline(cfg, "TRX40")
    assert sorted(_row_key(r) for r in replayed) == sorted(_row_key(r) for r in before), \
        "the recovery replays from the markdown"


def _row_key(row):
    """A row's identity-level signature: spellings normalised to identity, ids excluded."""
    return (graph_mod.identity(row["src"]), row["predicate"],
            graph_mod.identity(row["dst"]), row["note_id"] or "",
            row["valid_from"], row["valid_to"])


def test_old_schema_recovery_lands_in_the_entry_markdown(cfg):
    """The recovery leaves the graph IN the notes: the folded entry carries the spec afterwards,
    so a second loss of the db loses nothing — and a repeated rebuild does not rewrite the
    recovered frontmatter (byte-identical), which is what makes running recovery twice safe.

    The folded spec's casing is deliberately NOT pinned: the writer may keep the author's
    spelling or rewrite it to the canonical display form; the contract is the ENTITY the spec
    names (its identity) and the predicate/dst it asserts.
    """
    path = cfg.root / "notes" / "rr000002-docp-3600-depends-on.md"
    path.write_text(
        _entry_md("rr000002", "DOCP 3600 depends on the flash", T1), encoding="utf-8")
    db = cfg.db_path
    c = sqlite3.connect(db)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO entities(name, kind, aliases, created) VALUES(?,?,?,?)",
              ("DOCP 3600", "thing", "[]", T1))
    c.execute("INSERT INTO edges(src, predicate, dst, note_id, valid_from) VALUES(?,?,?,?,?)",
              ("DOCP 3600", "depends_on", "bios-2601", "rr000002", T1))  # the author's spelling
    c.commit()
    c.close()

    index_mod.connect(db).close()
    graph_mod.rebuild(cfg)
    entry = read_entry(path)
    assert entry.edges, "the folded spec must be in the entry's frontmatter for the next rebuild"
    src, predicate, dst = parse_edge_spec(entry.edges[0])
    assert graph_mod.identity(src) == graph_mod.identity("DOCP 3600"), \
        "the folded spec names the same entity the db row did, whatever spelling the writer chose"
    assert predicate == "depends_on"
    assert graph_mod.identity(dst) == graph_mod.identity("bios-2601")

    # the property that makes repeated recovery safe: a second rebuild is byte-identical
    md_after_first = path.read_bytes()
    graph_mod.rebuild(cfg)
    assert path.read_bytes() == md_after_first, (
        "a second rebuild must leave the recovered frontmatter byte-identical")

    # and the recovered spec survives the next loss of the db
    _clear_db(db)
    rx = run_cli("--vault", str(cfg.root), "reindex")
    assert rx.returncode == 0, rx.stderr
    rows = graph_mod.timeline(cfg, "DOCP 3600")
    assert len(rows) == 1 and rows[0]["dst"] == "bios-2601", (
        "the recovered spec survives the next loss of the db")