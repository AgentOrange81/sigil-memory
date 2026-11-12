"""SQLite index: full-text search over entries plus the temporal graph tables."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Entry, iter_entries

#: The schema version of this db, carried in `PRAGMA user_version`. A db written by an older
#: sigil has a lower number. The index is derived from the markdown, so an old db is a rebuild
#: away from being current - never a vault that cannot be opened (#23).
SCHEMA_VERSION = 2

#: TABLES FIRST, and no index in here: an index on a column that the migration has not added yet
#: is the crash that made every command die on a vault written before the identity key.
TABLES_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT, state TEXT, agent TEXT, project TEXT,
  session TEXT, created TEXT, confidence REAL, importance REAL, ask TEXT, summary TEXT,
  evidence TEXT, body TEXT, model TEXT, evidence_kind TEXT);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  summary, ask, evidence, body, content='entries', content_rowid='rowid',
  tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT DEFAULT 'thing',
  aliases TEXT DEFAULT '[]', created TEXT, key TEXT);

CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, predicate TEXT NOT NULL, dst TEXT NOT NULL,
  note_id TEXT, confidence REAL DEFAULT 1.0, valid_from TEXT, valid_to TEXT,
  UNIQUE(src, predicate, dst, valid_from));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""

#: INDEXES LAST, once every column they name exists.
INDEXES_SCHEMA = """
CREATE INDEX IF NOT EXISTS entities_key ON entities(key);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
"""

#: The old name, kept so anything that reached for it still finds the tables.
SCHEMA = TABLES_SCHEMA


def _migrate(c: sqlite3.Connection) -> None:
    """Add the columns an older db is missing, before any index needs them.

    A db written before the identity key existed has no `key` column, and `CREATE INDEX
    entities_key` on it raises `no such column: key`. The old `connect()` ran the whole schema
    script - indexes included - and only then asked whether the column existed, so the migration
    was unreachable exactly when it was needed and every command died on an old vault (#23).
    The order is now: tables, then columns, then indexes. The keys are backfilled here, so the
    db is usable the moment `connect()` returns.
    """
    cols = {r[1] for r in c.execute("PRAGMA table_info(entities)")}
    if "key" not in cols:
        c.execute("ALTER TABLE entities ADD COLUMN key TEXT")
    # #110: the model that wrote an entry arrived after the first vaults did. The column is nullable,
    # so an older row simply has no answer — which is the truth.
    entry_cols = {r[1] for r in c.execute("PRAGMA table_info(entries)")}
    if "model" not in entry_cols:
        c.execute("ALTER TABLE entries ADD COLUMN model TEXT")
    if "evidence_kind" not in entry_cols:
        c.execute("ALTER TABLE entries ADD COLUMN evidence_kind TEXT")
    from .graph import identity  # local import: graph imports index, so a top-level one cycles
    for row in c.execute("SELECT id, name FROM entities WHERE key IS NULL OR key = ''").fetchall():
        c.execute("UPDATE entities SET key = ? WHERE id = ?", (identity(row["name"]), row["id"]))


def refresh_if_stale(cfg) -> str | None:
    """A db written by an older schema is DERIVED: rebuild it from the markdown, never migrate it.

    Migration leaves rows whose display names no longer resolve (#37): `_migrate` backfills `key` while
    the entity keeps its old `name` spelling, so `resolve()` returns the canonical form and the join
    finds nothing — an old vault opens without crashing and silently reports an empty graph, which is
    better than the crash it used to be and worse than the truth. Rebuilding deletes that class of bug
    instead of fixing one instance; the price is a few milliseconds on a vault whose index was already
    wrong.

    Returns a one-line description of what happened, or None when the db is current (or absent).
    """
    db = Path(cfg.db_path)
    if not db.exists():
        return None
    try:
        probe = sqlite3.connect(db)
        try:
            version = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.DatabaseError:
        version = 0  # unreadable is worse than old: rebuild it too
    if version >= SCHEMA_VERSION:
        return None
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    n = reindex(cfg.root, db)
    from . import graph as graph_mod  # local import: graph imports index, so a top-level one cycles
    report = graph_mod.rebuild(cfg)
    graph_mod.materialize(cfg)
    return (f"index schema v{version} -> v{SCHEMA_VERSION}: rebuilt from markdown "
            f"({n} entries, {report['edges']} edges)")


def connect(db: str | Path) -> sqlite3.Connection:
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(TABLES_SCHEMA)
    _migrate(c)
    c.executescript(INDEXES_SCHEMA)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    c.commit()
    return c


def row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"], kind=row["kind"], state=row["state"], agent=row["agent"],
        project=row["project"], session=row["session"], created=row["created"],
        confidence=row["confidence"] or 0.0, importance=row["importance"] or 0.0,
        ask=row["ask"] or "", summary=row["summary"] or "", evidence=row["evidence"] or "",
    )


def reindex(root: str | Path, db: str | Path) -> int:
    rows = list(iter_entries(root))
    c = connect(db)
    with c:
        c.execute("DELETE FROM entries")
        c.execute("INSERT INTO entries_fts(entries_fts) VALUES('delete-all')")
        for path, e in rows:
            c.execute(
                "INSERT OR REPLACE INTO entries(id,path,kind,state,agent,project,session,created,"
                "confidence,importance,ask,summary,evidence,body,model,evidence_kind) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id, str(Path(path).relative_to(Path(root))), e.kind, e.state, e.agent, e.project,
                 e.session, e.created, e.confidence, e.importance, e.ask, e.summary, e.evidence, e.body,
                 e.model, e.evidence_kind),
            )
        c.execute("INSERT INTO entries_fts(rowid, summary, ask, evidence, body) "
                  "SELECT rowid, summary, ask, evidence, body FROM entries")
    c.close()
    return len(rows)


def _tokens(q: str) -> list[str]:
    return [t for t in "".join(ch if ch.isalnum() else " " for ch in q).split() if t]


def _fts_query(q: str) -> str:
    toks = _tokens(q)
    if not toks:
        return ""
    # the * must be outside the quotes: '"markdo*"' is a literal phrase, '"markdo"*' is a prefix
    return " AND ".join([f'"{t}"' for t in toks[:-1]] + [f'"{toks[-1]}"*'])


def _fts_query_or(q: str) -> str:
    """The same terms, ORed. BM25 ranks what comes back, so a query that shares words with a
    note still finds that note - the fallback for a question-shaped query, where one word
    ("why", "did", "fail") is in no note at all (#25)."""
    toks = _tokens(q)
    if not toks:
        return ""
    return " OR ".join([f'"{t}"' for t in toks[:-1]] + [f'"{toks[-1]}"*'])


#: bm25 weights, one per FTS column, in schema order: (summary, ask, evidence, body). FTS5 scores a
#: match wherever it lands, so before this an entry whose BODY happened to contain the query words
#: outranked the entry whose SUMMARY was the answer — the measured symptom was a hardware question
#: answered with a pasted transcript at rank 1 (#31). The summary is the entry's retrieval handle and
#: weighs most; `ask` next (it is the question the entry answers, which is what an agent's query looks
#: like), then evidence, then body. Tuned against `benchmarks/sweep_retrieval.py` (synthetic, 100
#: queries) and `benchmarks/live_queries.py` (the ten hand-judged questions that found the defect):
#: synthetic R@5 0.640 / R@10 0.900 / MRR 0.318 against 0.640 / 0.870 / 0.320 for the unweighted
#: baseline, and live rank-1 correct 9/10 against 5/10. A sharper weight set scored higher on the live
#: set and worse on the harness; the harness wins ties because ten hand-judged questions overfit.
COLUMN_WEIGHTS = {"summary": 10.0, "ask": 4.0, "evidence": 2.0, "body": 0.5}

#: How far past the caller's `limit` each query shape may scan before the thin-AND top-up. Wide enough
#: that a relevant OR hit does not have to be in the first `limit` of the loose ranking, narrow enough
#: that the cost stays a handful of rows on a corpus of thousands.
CANDIDATE_WINDOW = 4


def _scan(c: sqlite3.Connection, match: str, where: list[str], params: list, limit: int) -> list[dict]:
    """One bm25 query with the column weights applied. `match` is a whole FTS expression."""
    clauses = ["entries_fts MATCH ?"] + where
    sql = ("SELECT e.*, snippet(entries_fts, 0, '<<', '>>', '…', 14) AS snip, "
           "bm25(entries_fts, ?, ?, ?, ?) AS score "
           "FROM entries_fts JOIN entries e ON e.rowid = entries_fts.rowid "
           "WHERE " + " AND ".join(clauses) + " ORDER BY score LIMIT ?")
    weights = [COLUMN_WEIGHTS["summary"], COLUMN_WEIGHTS["ask"], COLUMN_WEIGHTS["evidence"],
               COLUMN_WEIGHTS["body"]]
    return [dict(r) for r in c.execute(sql, weights + [match] + params + [limit]).fetchall()]


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def time_bound(value: str, *, upper: bool) -> tuple[str, str]:
    """Turn a `--since` / `--until` value into (sql operator, ISO instant) over `entries.created`.

    A bare date names a DAY, not an instant — that is the whole point of the flag. `since 2026-10-01`
    starts at 00:00:00Z; `until 2026-10-29` runs THROUGH the 29th, so it becomes a `< next midnight`
    bound rather than `<= 00:00`. Getting this backwards silently drops a day, and every entry in a
    working vault is created in the afternoon.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty time bound")
    try:
        if _DAY_RE.match(raw):
            start = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if upper:
                return "<", (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return ">=", start.strftime("%Y-%m-%dT%H:%M:%SZ")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{raw!r} is not a date or an ISO instant — use 2026-10-01 or 2026-10-01T12:30:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    instant = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("<" if upper else ">="), instant


def search(db: str | Path, query: str, kind: str | None = None, state: str | None = None,
           agent: str | None = None, project: str | None = None, limit: int = 10,
           since: str | None = None, until: str | None = None) -> list[dict]:
    """Rank entries for `query`. AND first, then topped up from the OR ranking (#43).

    Every token is ANDed, which is precise and, for a question-shaped query, nearly empty: one word
    ("why", "did") in no note at all used to produce nothing, and the OR fallback that fixed that
    (#25) only ran on an EMPTY result. One rare token sitting in one unrelated note therefore
    satisfied the AND, the fallback never fired, and the caller got a one-row answer that looked
    complete. The AND ranking still comes first — a note matching every token beats a note sharing
    one — but when it is shorter than the caller asked for, the remaining slots are filled from the
    OR ranking, so the answer is somewhere in the list instead of absent from it.
    """
    c = connect(db)
    match, loose = _fts_query(query), _fts_query_or(query)
    if not match:
        c.close()
        return []
    where: list[str] = []
    params: list = []
    for col, val in (("kind", kind), ("state", state), ("agent", agent), ("project", project)):
        if val:
            where.append(f"e.{col} = ?")
            params.append(val)
    for value, is_upper in ((since, False), (until, True)):
        if value:
            op, instant = time_bound(str(value), upper=is_upper)
            where.append(f"e.created {op} ?")
            params.append(instant)
    window = max(limit * CANDIDATE_WINDOW, limit)
    try:
        rows = _scan(c, match, where, params, window)
        if loose and loose != match and len(rows) < limit:
            seen = {r["id"] for r in rows}
            rows += [r for r in _scan(c, loose, where, params, window) if r["id"] not in seen]
    finally:
        c.close()
    rows.sort(key=lambda r: r["score"])
    return rows[:limit]


def stats(db: str | Path) -> dict:
    c = connect(db)
    total = c.execute("SELECT count(*) FROM entries").fetchone()[0]
    by_state = {r[0]: r[1] for r in c.execute("SELECT state, count(*) FROM entries GROUP BY state")}
    by_kind = {
        r[0]: r[1]
        for r in c.execute("SELECT kind, count(*) FROM entries GROUP BY kind ORDER BY 2 DESC")
    }
    ents = c.execute("SELECT count(*) FROM entities").fetchone()[0]
    edges = c.execute("SELECT count(*) FROM edges").fetchone()[0]
    live = c.execute("SELECT count(*) FROM edges WHERE valid_to IS NULL").fetchone()[0]
    c.close()
    return {"entries": total, "by_state": by_state, "by_kind": by_kind,
            "entities": ents, "edges": edges, "edges_live": live, "edges_closed": edges - live}
