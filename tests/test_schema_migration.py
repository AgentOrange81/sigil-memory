"""The schema migration (#23) and the graph-in-the-markdown round trip (#24), plus the stale-index
rebuild (#37). Run them with the rest of the suite: `pytest`."""
import sqlite3

from sigil import graph as graph_mod
from sigil import index as index_mod
from sigil.config import load
from sigil.store import Entry, write_entry

#: The schema as it was BEFORE the identity key existed: `entities` has no `key` column, and
#: there is no `user_version` to compare against. This is the db that made every command die.
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


def _old_db(tmp_path):
    db = tmp_path / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO entities(name, kind, aliases, created) VALUES(?,?,?,?)",
              ("Next.js", "thing", "[]", "2026-01-01T00:00:00Z"))
    c.execute("INSERT INTO edges(src, predicate, dst, note_id, valid_from) VALUES(?,?,?,?,?)",
              ("quipu", "depends_on", "sqlite", "", "2026-01-01T00:00:00Z"))
    c.commit()
    c.close()
    return db


def test_connect_recovers_from_a_db_written_before_the_identity_key(tmp_path):
    """The old `connect()` ran CREATE INDEX entities_key before the column existed, so it raised
    `no such column: key` and every command died on an old vault. It must open now."""
    db = _old_db(tmp_path)
    conn = index_mod.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
    assert "key" in cols, "the key column was not added"
    # the keys are backfilled, so the db is usable the moment connect() returns
    row = conn.execute("SELECT key FROM entities WHERE name = ?", ("Next.js",)).fetchone()
    assert row[0] == "nextjs", f"the key was not backfilled: {row[0]!r}"
    # and the index that used to kill it exists now
    idx = [r[1] for r in conn.execute("PRAGMA index_list(entities)").fetchall()]
    assert "entities_key" in idx
    # the old edges are still there: a migration is not a rebuild
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
    conn.close()


def test_connect_records_the_schema_version(tmp_path):
    db = tmp_path / "index.db"
    conn = index_mod.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == index_mod.SCHEMA_VERSION
    conn.close()
    # and an old db is brought up to the current version, not left behind
    old = _old_db(tmp_path / "old")
    conn = index_mod.connect(old)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == index_mod.SCHEMA_VERSION
    conn.close()


def test_a_db_from_an_older_schema_is_rebuilt_from_the_markdown_not_migrated(tmp_path):
    """#37: after migration the rows keep their old display names, so `timeline` reported an EMPTY
    graph for a vault whose markdown holds the edge. A pre-current `user_version` is a derived index,
    not history: rebuild it and the class of bug goes away instead of one instance of it."""
    cfg = load(tmp_path / "vault", create=True)
    e = Entry(kind="fact", state="promoted", summary="TRX40 is measured as DOCP 3600",
              evidence="bench", edges=["TRX40:measured_as:DOCP 3600"],
              created="2026-01-01T00:00:00Z")
    write_entry(cfg.root, e)

    # an index from before the identity key: no `key`, no user_version, rows that no longer resolve
    db = cfg.db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO entities(name, kind, aliases, created) VALUES(?,?,?,?)",
              ("TRX40", "thing", "[]", "2026-01-01T00:00:00Z"))
    c.execute("INSERT INTO edges(src, predicate, dst, note_id, valid_from) VALUES(?,?,?,?,?)",
              ("trx40", "measured_as", "docp 3600", e.id, "2026-01-01T00:00:00Z"))
    c.commit()
    c.close()

    note = index_mod.refresh_if_stale(cfg)
    assert note and "rebuilt from markdown" in note, note
    rows = graph_mod.timeline(cfg, "TRX40")
    assert len(rows) == 1, f"the rebuild must find the edge the migration could not resolve: {rows}"
    assert rows[0]["predicate"] == "measured_as"

    # a current db is left alone: this is a recovery path, not a rebuild on every command
    assert index_mod.refresh_if_stale(cfg) is None


def test_the_graph_survives_the_loss_of_the_db(tmp_path):
    """`rm .sigil/index.db` must lose nothing: the edges and the closed windows are in the notes."""
    cfg = load(tmp_path / "vault", create=True)
    e = Entry(kind="decision", state="promoted", summary="TRX40 is measured as DOCP 3200",
              evidence="bench", edges=["TRX40:measured_as:DOCP 3200"], created="2026-01-01T00:00:00Z")
    write_entry(cfg.root, e)
    graph_mod.add_edge(cfg, "TRX40", "measured_as", "DOCP 3200", note_id=e.id,
                       at="2026-01-01T00:00:00Z")
    before = graph_mod.timeline(cfg, "TRX40")
    graph_mod.invalidate(cfg, before[0]["id"], at="2026-06-01T00:00:00Z")
    expected = graph_mod.timeline(cfg, "TRX40")
    assert expected[0]["valid_to"] == "2026-06-01T00:00:00Z"

    # the db goes; the markdown stays
    cfg.db_path.unlink()
    report = graph_mod.rebuild(cfg)
    assert report["edges"] == 1
    assert report["closed"] == 1
    after = graph_mod.timeline(cfg, "TRX40")
    assert [(r["src"], r["predicate"], r["dst"], r["valid_from"], r["valid_to"]) for r in after] == \
           [(r["src"], r["predicate"], r["dst"], r["valid_from"], r["valid_to"]) for r in expected]
    assert after[0]["valid_to"] == "2026-06-01T00:00:00Z", "a closed window came back to life"
