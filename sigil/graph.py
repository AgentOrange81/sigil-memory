"""Temporal entity/relation graph.

Edges are never deleted. Asserting a new edge that contradicts an old one closes the old edge
(`valid_to`) and opens a new one, so "what did we believe in June, and why did it change" stays
answerable — and the pair becomes preference data on export.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from .config import Config
from .index import connect
from .store import (
    Entry,
    find_entry,
    format_edge_spec,
    iter_entries,
    now_iso,
    parse_edge_spec,
    read_entry,
)


def slug(name: str) -> str:
    """Display form: lowercase, punctuation collapsed to single hyphens. NOT the identity."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "entity"


def identity(name: str) -> str:
    """The identity key: case-, width-, separator- and accent-insensitive, in ANY script.

    `slug()` alone is not enough and this was a live bug: "Next.js" -> next-js while "nextjs" ->
    nextjs, so one entity became two nodes — precisely what the old docstring claimed to prevent.
    Slugging keeps separators, so every punctuation variant is a new node. The key strips them, so
    "Next.js", "nextjs" and "Next JS" resolve to one entity whose display name is whichever spelling
    was seen first, with the rest kept as aliases.

    A *version* is a different key on purpose: "Next.js 16" -> nextjs16 stays separate, because a
    version qualifier is a property of the assertion, not of the entity.

    Letters and digits of EVERY script are kept. The old key folded them away — "日本語" and "中文"
    both became "unknown", so two different entities collapsed into one node. Only a name with no
    letter or digit at all falls back, and then to a digest of the raw name, so two such names never
    share a key either.
    """
    raw = name or ""
    folded = unicodedata.normalize("NFKD", raw.casefold())
    key = "".join(ch for ch in folded if unicodedata.category(ch)[0] in ("L", "N"))
    if key:
        return key
    return "unknown-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def edge_key(src: str, predicate: str, dst: str) -> tuple[str, str, str]:
    """The identity of a claim: predicate plus both endpoints under `identity()`.

    Both surfaces spell entities their own way and all three spellings are correct in context:
    the markdown keeps the model's prose ("GPU 1"), `add_edge` stores the slug ("gpu-1"), and a
    rebuild can store the identity key ("gpu1"). Comparing spellings reported nine live edges as
    "in the index but not in the markdown" that the markdown did record (#115), and a rebuild then
    minted a second row for each. Comparing identities is the only comparison that is stable under
    all three: `identity()` already exists to decide when two names are one entity.
    """
    return (predicate, identity(src), identity(dst))


def display_name(name: str) -> str:
    """The display form of a name: its ASCII slug when it has one, itself when it has not.

    `slug()` collapses every non-ASCII name to "entity": two different Japanese names would then
    share a display name and collide on the UNIQUE(name) column. A name with no ASCII letter or
    digit keeps itself, so it stays readable and stays distinct.
    """
    text = (name or "").strip()
    if any(ch.isascii() and ch.isalnum() for ch in text):
        return slug(text)
    return text or "entity"


def hub_stem(name: str, key: str) -> str:
    """The file name of an entity hub: its slug, or a digest when the slug is the generic fallback.

    Two non-Latin entities both slug to "entity", so without this they would overwrite each other's
    hub file.
    """
    text = (name or "").strip()
    if any(ch.isascii() and ch.isalnum() for ch in text):
        return slug(text)
    return "entity-" + hashlib.sha256((key or text).encode("utf-8")).hexdigest()[:8]


def canonical(name: str) -> str:
    return slug(name)


def ensure_entity(c: sqlite3.Connection, name: str, kind: str = "thing", aliases=()) -> str:
    """Create or find the entity by IDENTITY, return its display name."""
    name = (name or "").strip()
    key = identity(name)
    disp = display_name(name)
    row = c.execute("SELECT name, aliases, key FROM entities WHERE key = ? OR name = ?",
                    (key, disp)).fetchone()
    if row:
        stored, raw_aliases = row[0], row[1]
        seen = set(json.loads(raw_aliases or "[]"))
        if name != stored:
            seen.add(name)  # keep every spelling we saw, never as a second node
        c.execute("UPDATE entities SET aliases = ?, key = ? WHERE name = ?",
                  (json.dumps(sorted(seen), ensure_ascii=False), key, stored))
        return stored
    extra = sorted({*aliases, *([name] if name != disp else [])})
    c.execute("INSERT INTO entities(name, kind, aliases, created, key) VALUES(?,?,?,?,?)",
              (disp, kind, json.dumps(extra, ensure_ascii=False), now_iso(), key))
    return disp


def resolve(cfg: Config, name: str) -> str:
    """Map any spelling to the form edge rows are written in (queries must go through this).

    Rows are written with the name `ensure_entity` remembers (one name per identity, so 'Next.js'
    and 'nextjs' cannot become two nodes), while a migrated vault's older rows keep whatever
    spelling they were written with. Both are the same claim, so the queries below build a candidate
    set from every spelling this vault knows and then compare by `identity()` (#122).
    """
    c = connect(cfg.db_path)
    try:
        row = c.execute("SELECT name FROM entities WHERE key = ? OR name = ?",
                        (identity(name), display_name(name))).fetchone()
    finally:
        c.close()
    return row[0] if row else display_name(name)


def _spellings(cfg: Config, name: str) -> list[str]:
    """Every spelling this vault might have written for one identity.

    Used to build the SQL candidate set; the rows are then filtered by `identity()`, because a
    migration cannot rewrite history and a query that trusts one spelling reports an empty graph
    (#122).
    """
    spellings = {name, display_name(name)}
    c = connect(cfg.db_path)
    try:
        row = c.execute("SELECT name, aliases FROM entities WHERE key = ? OR name = ?",
                        (identity(name), display_name(name))).fetchone()
    finally:
        c.close()
    if row:
        spellings.add(row[0])
        try:
            spellings.update(json.loads(row[1] or "[]"))
        except ValueError:
            pass
    return sorted(s for s in spellings if s)


def _identity_rows(rows, spellings: list[str]) -> list[dict]:
    """Rows that touch this identity, judged by the vault's OWN membership, not by string equality.

    `identity()` alone is not enough to filter here, and the reason is worth remembering: the stored
    name is `display_name()` of the first spelling seen, and that drops what it cannot romanise —
    `Café` is stored as `caf`, whose identity is `caf`, not `cafe`. The entities table keeps the
    spelling and the key, so IT is the authority on which strings are one node; this keeps the rows
    that a spelling list says belong.
    """
    pool = set(spellings)
    return [dict(r) for r in rows if r["src"] in pool or r["dst"] in pool]


# --- the graph lives in the markdown (#24) ------------------------------------------------
#
# `rm .sigil/index.db` must lose nothing: the notes are the truth, and the db is derived from
# them. So an asserted edge is written into the entry that asserted it, and a closed window is
# written into that same entry. Without this the temporal graph - the most valuable record in the
# system - existed in exactly one place, and that place was the file the design calls disposable.


def _update_entry(cfg: Config, note_id: str, mutate) -> bool:
    """Apply `mutate` to the entry with this id and write it back to the same file."""
    path = find_entry(cfg.root, note_id) if note_id else None
    if path is None:
        return False
    e = read_entry(path)
    if not mutate(e):
        return False
    path.write_text(e.to_markdown(), encoding="utf-8")
    return True


def record_edge(cfg: Config, note_id: str, spec: str) -> bool:
    """Write an asserted edge into the entry that asserted it. Idempotent."""

    def mutate(e: Entry) -> bool:
        if spec in e.edges:
            return False
        e.edges = [*e.edges, spec]
        return True

    return _update_entry(cfg, note_id, mutate)


def record_closure(cfg: Config, note_id: str, src: str, predicate: str, dst: str, at: str,
                    replacement: str = "") -> bool:
    """Write a closed window into the entry that asserted the edge. Idempotent.

    `replacement` is the spec of the edge that took over. A contradiction closes one window and
    opens another at the same instant, so the closure has to say which edge starts when it does:
    without that, a replay dates the replacement to the entry's creation and the rebuilt timeline
    is not the timeline that was there before.
    """
    spec = format_edge_spec(src, predicate, dst)

    def mutate(e: Entry) -> bool:
        for i, c in enumerate(e.edge_closures):
            if c.get("spec") == spec and c.get("at") == at:
                if replacement and not c.get("replacement"):
                    e.edge_closures = [*e.edge_closures[:i], {**c, "replacement": replacement},
                                       *e.edge_closures[i + 1:]]
                    return True
                return False
        record = {"spec": spec, "at": at}
        if replacement:
            record["replacement"] = replacement
        e.edge_closures = [*e.edge_closures, record]
        return True

    return _update_entry(cfg, note_id, mutate)


def carrier_entries(entries: dict[str, Entry], src: str, note_id: str) -> list[Entry]:
    """The entries that could carry this edge, in order of authority (#96).

    `src` is the id of the entry that asserted it — the caller used that id as the entity name — and
    `note_id` is what the row claims. Both can be prose, because that is the field a bad caller
    filled with a summary, so every caller must ask "does any candidate exist" and "does any
    candidate record the spec" rather than trusting one column. `audit`, `repair --edges` and the
    rebuild's fold phase share this, or they disagree about the same vault.
    """
    out: list[Entry] = []
    for eid in (src, note_id or ""):
        entry = entries.get(eid) if eid else None
        if entry is not None and all(entry.id != e.id for e in out):
            out.append(entry)
    return out


def entry_records_edge(entry: Entry, spec: str) -> bool:
    """Does the entry already record this edge? Compare DISPLAY forms: the db stores displays,
    so that is what a spec resolves to."""
    try:
        src, predicate, dst = parse_edge_spec(spec)
    except ValueError:
        return False
    key = edge_key(src, predicate, dst)
    for raw in entry.edges:
        try:
            s2, p2, d2 = parse_edge_spec(raw)
        except ValueError:
            continue
        if edge_key(s2, p2, d2) == key:
            return True
    return False


def _require_carrier(cfg: Config, note_id: str, verb: str) -> Path | None:
    """The entry that must carry this edge, or None when no carrier is claimed.

    An ABSENT carrier stays legal: `sigil edge a part_of b` with no `--note` is a documented
    form, and it warns on stderr instead of refusing.

    A carrier that is GIVEN but names no entry is a usage error, and it must be refused before
    anything is written. The old path handed `note_id` to `record_edge()`, whose `find_entry()`
    returned None, so `_update_entry` returned False and nobody was told: the edge existed only
    in SQLite, and `rm .sigil/index.db` + `reindex` deleted a live belief (#94). A refusal that
    writes nothing is the only safe one - a row without a carrier is the defect itself.

    The message names the carrier verbatim, the fix (pass the entry id) and the recovery for a
    vault that already holds such an edge (`sigil repair --edges`).
    """
    if not note_id:
        return None
    try:
        path = find_entry(cfg.root, note_id)
    except (OSError, ValueError):      # a carrier with glob characters in it is not an entry
        path = None
    if path is not None:
        return path
    raise ValueError(
        f"{verb}: carrier {note_id!r} names no entry; pass the entry id of the note that asserts "
        f"this edge (the 8-hex id of an existing entry), or run `sigil repair --edges` to re-home "
        f"the edges the index holds but the markdown does not record"
    )


def _check_predicate(cfg: Config, predicate: str, allow_new_predicate: bool = False) -> None:
    """A predicate outside the closed vocabulary is a hard error — unless it is let through."""
    if predicate not in cfg.predicates and not allow_new_predicate:
        raise ValueError(
            f"predicate {predicate!r} is not in the vocabulary; add it to {cfg.config_path} "
            f"or pass allow_new_predicate=True"
        )


def _assert_edge(c: sqlite3.Connection, src: str, predicate: str, dst: str, note_id: str,
                 confidence: float, at: str) -> tuple[int, bool]:
    """Open an edge on an open connection and return (id, created). No commit of its own."""
    src = ensure_entity(c, src)   # returns the display name for this identity
    dst = ensure_entity(c, dst)
    # Idempotent by design, in two shapes. Same second: the UNIQUE constraint below. Later
    # instant: a new valid_from, which used to create a SECOND live edge for the same
    # (src, predicate, dst) — a slow duplicate, found by CI. A live edge for this triple means
    # the fact is already asserted, so keep the first and return its id.
    live = c.execute(
        "SELECT id FROM edges WHERE src = ? AND predicate = ? AND dst = ? AND valid_to IS NULL",
        (src, predicate, dst),
    ).fetchone()
    if live:
        return live[0], False
    c.execute(
        "INSERT INTO edges(src, predicate, dst, note_id, confidence, valid_from, valid_to) "
        "VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(src, predicate, dst, valid_from) DO NOTHING",
        (src, predicate, dst, note_id, confidence, at),
    )
    eid = c.execute(
        "SELECT id FROM edges WHERE src = ? AND predicate = ? AND dst = ? AND valid_from = ?",
        (src, predicate, dst, at),
    ).fetchone()[0]
    c.execute("INSERT INTO events(at, kind, detail) VALUES(?,?,?)",
              (at, "assert", json.dumps({"edge": eid, "src": src, "pred": predicate, "dst": dst,
                                        "note": note_id}, ensure_ascii=False)))
    created = c.execute(
        "SELECT count(*) FROM edges WHERE src = ? AND predicate = ? AND dst = ? AND valid_from = ?",
        (src, predicate, dst, at),
    ).fetchone()[0] == 1
    return eid, created


def _entry_created(cfg: Config, note_id: str) -> str:
    """The created instant of the entry that carries an edge — the only timestamp the markdown has.

    An edge asserted by an entry starts when that entry was created. The markdown is the truth and
    the db is derived from it, so the start of a window has to be readable from the notes: an
    edge dated to the moment it was asserted would be dated differently by every rebuild.
    """
    if not note_id:
        return ""
    path = find_entry(cfg.root, note_id)
    if path is None:
        return ""
    return read_entry(path).created or ""


def add_edge(cfg: Config, src: str, predicate: str, dst: str, note_id: str = "",
             confidence: float = 1.0, at: str | None = None,
             allow_new_predicate: bool = False, record: bool = True) -> int:
    """Assert src -predicate-> dst. Returns the new edge id.

    Raises ValueError for a predicate outside the closed vocabulary unless allow_new_predicate —
    an open predicate space is how a graph rots into 400 spellings of "uses".

    The spec is written into the entry that asserted it, so the graph survives the loss of the db
    (#24). `record=False` is for the rebuild, which replays what the markdown already says.
    """
    _check_predicate(cfg, predicate, allow_new_predicate)
    # The carrier is resolved BEFORE the connection opens: a refused assertion must leave the db
    # exactly as it was - no row, no event (#94). `record=False` is exempt on purpose: it asserts
    # nothing about the markdown, it PROMISES the markdown already says this (the rebuild and
    # identity-level callers), so there is no carrier left unrecorded by it.
    if record:
        _require_carrier(cfg, note_id, "edge")
    at = at or _entry_created(cfg, note_id) or now_iso()
    c = connect(cfg.db_path)
    try:
        with c:
            eid, _created = _assert_edge(c, src, predicate, dst, note_id, confidence, at)
    finally:
        c.close()
    if record and note_id:
        record_edge(cfg, note_id, format_edge_spec(src, predicate, dst))
    return eid


def contradict(cfg: Config, old_edge_id: int, src: str, predicate: str, dst: str,
               note_id: str = "", confidence: float = 1.0, at: str | None = None) -> int:
    """Close `old_edge_id` and assert its replacement in ONE transaction.

    The replacement inherits the old edge's valid_from for query purposes only if `at` is omitted —
    no: it starts now. History is preserved because the old row keeps its window.

    The replacement is validated BEFORE the old edge is closed, and both writes share one
    transaction: a rejected replacement (a predicate outside the vocabulary, a destination that
    names nothing) must leave the old belief live. Closing first and validating after left the
    graph with a closed belief and no live edge at all.

    The CARRIER is validated on the same terms, and even earlier: the row is read first, the
    carrier the replacement would use (`note_id or row["note_id"]`) is resolved, and only then does
    the transaction open (#94). A replacement that cannot be carried is refused while the old
    belief is still live - the alternative was an edge in the db with no entry to carry it, which
    `rm index.db` + `reindex` would delete.
    """
    _check_predicate(cfg, predicate)
    if not (dst or "").strip():
        raise ValueError("contradict needs a destination to believe in")
    if not (src or "").strip():
        raise ValueError("contradict needs a subject to believe about")
    c = connect(cfg.db_path)
    try:
        row = c.execute("SELECT id, src, predicate, dst, note_id FROM edges WHERE id = ?",
                        (old_edge_id,)).fetchone()
        if row is None:
            raise ValueError(f"no edge {old_edge_id}")
        # The replacement is carried by an entry, or a rebuild drops it: an edge that exists
        # only in the db is a live belief that vanishes, which is worse than one that comes
        # back. With no note given, the entry that owned the closed edge carries it, so that
        # entry's frontmatter ends up with the new spec in `edges:` AND the closure in
        # `edge_closures:` - a replay then reproduces both windows. An explicit --note wins.
        carrier = note_id or (row["note_id"] or "")
        # resolved BEFORE the transaction opens: a refused replacement closes nothing (#94)
        _require_carrier(cfg, carrier, "contradict")
        at = at or now_iso()
        with c:
            c.execute("UPDATE edges SET valid_to = ? WHERE id = ? AND valid_to IS NULL", (at, old_edge_id))
            c.execute("INSERT INTO events(at, kind, detail) VALUES(?,?,?)",
                      (at, "invalidate", json.dumps({"edge": old_edge_id})))
            eid, created = _assert_edge(c, src, predicate, dst, carrier, confidence, at)
    finally:
        c.close()
    # the closed window belongs to the entry that asserted the old edge; the new one belongs to
    # the entry that carries the correction. Both go into the markdown, or a rebuild would
    # resurrect the closed belief and lose the correction.
    #
    # If the assertion reused an edge that was already live, no replacement was created: that
    # edge keeps its own window, and naming it as the replacement would make a replay re-date it.
    replacement = format_edge_spec(src, predicate, dst) if created else ""
    if row["note_id"]:
        record_closure(cfg, row["note_id"], row["src"], row["predicate"], row["dst"], at,
                       replacement=replacement)
    if carrier and created:
        record_edge(cfg, carrier, replacement)
    return eid


def _asserted_triples(entry: Entry) -> set[tuple[str, str, str]]:
    """The (predicate, display src, display dst) triples an entry asserts. Bad specs are ignored."""
    out: set[tuple[str, str, str]] = set()
    for spec in entry.edges:
        try:
            src, predicate, dst = parse_edge_spec(spec)
        except ValueError:
            continue
        out.add(edge_key(src, predicate, dst))
    return out


def close_superseded(cfg: Config, old_ids, new_entry: Entry, at: str | None = None) -> list[dict]:
    """Close the live edges of every entry this one supersedes (#20).

    `--supersedes` records the preference pair but used to leave the old belief's window open, so
    `timeline` and `export graph` showed two live windows for one fact and the stale claim was
    indistinguishable from the current one. Closing is deliberate, dated, and written into the
    markdown: the closure goes into the entry that asserted the edge (`record_closure`), so
    `rm .sigil/index.db` + `reindex` reproduces the closed window instead of resurrecting it.

    An edge the new entry asserts ITSELF is left open on purpose. `_assert_edge` is idempotent by
    (src, predicate, dst), so re-asserting a live triple reuses that edge; closing it here would
    leave the vault with the fact closed and nothing live — a rebuild that loses a belief. Superseding
    an entry while restating its edge means the belief continues, and only its carrier changed.

    Returns the windows closed, as dicts, so a caller can report them.
    """
    at = at or new_entry.created or now_iso()
    keep = _asserted_triples(new_entry)
    closed: list[dict] = []
    for old_id in dict.fromkeys(str(o) for o in (old_ids or []) if str(o).strip()):
        c = connect(cfg.db_path)
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT id, src, predicate, dst, valid_from FROM edges "
                "WHERE note_id = ? AND valid_to IS NULL ORDER BY id", (old_id,)).fetchall()]
        finally:
            c.close()
        for row in rows:
            triple = edge_key(row["src"], row["predicate"], row["dst"])
            if triple in keep:
                continue
            if (row["valid_from"] or "") > at:
                # a window cannot close before it opens (a hand-written `created` in the future);
                # leave it rather than write a negative window into the history
                continue
            c = connect(cfg.db_path)
            try:
                with c:
                    cur = c.execute("UPDATE edges SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                                    (at, row["id"]))
                    if cur.rowcount:
                        c.execute("INSERT INTO events(at, kind, detail) VALUES(?,?,?)",
                                  (at, "invalidate",
                                   json.dumps({"edge": row["id"]}, ensure_ascii=False)))
                changed = cur.rowcount
            finally:
                c.close()
            if not changed:
                continue
            # the closed window belongs to the entry that asserted the edge; without this line the
            # rebuild would resurrect the superseded belief
            record_closure(cfg, old_id, row["src"], row["predicate"], row["dst"], at)
            closed.append({"edge": row["id"], "spec": format_edge_spec(row["src"], row["predicate"],
                                                                      row["dst"]),
                           "superseded": old_id, "at": at})
    return closed


def invalidate(cfg: Config, edge_id: int, at: str | None = None) -> None:
    at = at or now_iso()
    c = connect(cfg.db_path)
    try:
        with c:
            row = c.execute("SELECT src, predicate, dst, note_id FROM edges WHERE id = ?",
                            (edge_id,)).fetchone()
            cur = c.execute("UPDATE edges SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                            (at, edge_id))
            if cur.rowcount == 0:
                # raising inside `with c` rolls back; never close the handle before the block exits
                raise ValueError(f"no live edge {edge_id}")
            c.execute("INSERT INTO events(at, kind, detail) VALUES(?,?,?)",
                      (at, "invalidate", json.dumps({"edge": edge_id}, ensure_ascii=False)))
    finally:
        c.close()
    # a closed window that is not written down is a belief that comes back on the next rebuild
    if row is not None and row["note_id"]:
        record_closure(cfg, row["note_id"], row["src"], row["predicate"], row["dst"], at)


def timeline(cfg: Config, entity: str, at: str | None = None) -> list[dict]:
    """Edges touching `entity`, optionally as known at a point in time."""
    spellings = _spellings(cfg, entity)
    marks = ",".join("?" * len(spellings))
    c = connect(cfg.db_path)
    try:
        if at:
            rows = c.execute(
                f"SELECT * FROM edges WHERE (src IN ({marks}) OR dst IN ({marks})) "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ORDER BY valid_from",
                (*spellings, *spellings, at, at),
            ).fetchall()
        else:
            rows = c.execute(
                f"SELECT * FROM edges WHERE src IN ({marks}) OR dst IN ({marks}) "
                "ORDER BY valid_from", (*spellings, *spellings),
            ).fetchall()
    finally:
        c.close()
    return _identity_rows(rows, spellings)


def project_entries(cfg: Config, name: str, include_pruned: bool = False) -> list[dict]:
    """Entries tagged with a project, as a hub view over the graph (#85).

    A project hub could be 466 persisted edges, one per tagged entry, written into 466 markdown
    files — and then `rebuild` would fold them back and forth, because the db is derived. The tag IS
    the assertion and it is already in the markdown, so the hub is a VIEW: entries whose `project`
    matches by identity (so "Shitter" and "shitter" are one hub), no writes, no duplication, nothing
    to keep in sync. It answers the question the persisted edges were for — everything about a
    project — and it cannot drift, because there is nothing to drift from.

    `pruned` entries are left out unless asked for: pruning is the vault's decision that something is
    not knowledge, and a browse surface that leads with rejected material teaches an agent to cite it.
    `include_pruned=True` is for audit work, not for recall.
    """
    key = identity(name)
    if not key or key == identity("unknown"):
        return []
    rows = []
    for _path, e in iter_entries(cfg.root):
        if not include_pruned and e.state == "pruned":
            continue
        if e.project and identity(e.project) == key:
            rows.append({"id": e.id, "summary": e.summary, "state": e.state, "kind": e.kind,
                         "created": e.created})
    rows.sort(key=lambda r: (r["created"] or "", r["id"]))
    return rows


#: An entry id is 8 hex characters, and it is how one entry points at another in its own prose.
_ENTRY_ID_RE = re.compile(r"\b([0-9a-f]{8})\b")


def citations(cfg: Config, entry_id: str) -> dict:
    """Which entries this one cites, and which cite it — derived, never persisted (#85, option 2).

    A citation is an observation about the text, not additional knowledge: `[[e2314ae5]]` in a body is
    reproducible from that body forever, so writing it as an edge would duplicate a derivable fact into
    markdown that `rebuild` then re-folds. The view answers the question the edges were for — what else
    in the vault points at this — and it cannot drift.
    """
    entries = [(p, e) for p, e in iter_entries(cfg.root)]
    known = {e.id for _p, e in entries}
    cites: list[str] = []
    cited_by: list[str] = []
    for _path, e in entries:
        text = (e.body or "") + " " + (e.summary or "") + " " + " ".join(e.edges or [])
        refs = {m for m in _ENTRY_ID_RE.findall(text) if m in known and m != e.id}
        if not refs:
            continue
        if e.id == entry_id:
            cites = sorted(refs)
        elif entry_id in refs:
            cited_by.append(e.id)
    return {"entry": entry_id, "cites": cites, "cited_by": sorted(cited_by)}


def connectivity(cfg: Config) -> dict:
    """How much of the corpus the graph actually reaches — the number #85 was filed on."""
    c = connect(cfg.db_path)
    try:
        by_predicate = {r["predicate"]: r["n"] for r in c.execute(
            "SELECT predicate, count(*) AS n FROM edges WHERE valid_to IS NULL "
            "GROUP BY predicate ORDER BY n DESC").fetchall()}
        names = {r[0] for r in c.execute(
            "SELECT src FROM edges WHERE valid_to IS NULL UNION SELECT dst FROM edges "
            "WHERE valid_to IS NULL").fetchall()}
        notes = {r[0] for r in c.execute(
            "SELECT note_id FROM edges WHERE valid_to IS NULL "
            "AND note_id IS NOT NULL AND note_id != ''").fetchall()}
    finally:
        c.close()
    entries = [e for _p, e in iter_entries(cfg.root)]
    projects: dict[str, int] = {}
    for e in entries:
        if e.project and identity(e.project) != identity("unknown"):
            projects[e.project] = projects.get(e.project, 0) + 1
    return {"edges_by_predicate": by_predicate, "nodes": len(names),
            "entries_with_an_edge": len(notes), "entries": len(entries),
            "projects": dict(sorted(projects.items(), key=lambda kv: -kv[1])[:10])}


def neighbors(cfg: Config, entity: str, depth: int = 1, include_pruned: bool = False) -> dict:
    """BFS over live edges. Returns {"nodes": [...], "edges": [...], "entries": [...]}.

    `entries` is the project-hub view (#85) and is empty for an entity that is not a project tag.
    """
    entity = resolve(cfg, entity)
    seen = {identity(entity)}
    edges: list[dict] = []
    frontier = [entity]
    c = connect(cfg.db_path)
    for _ in range(max(1, depth)):
        nxt: list[str] = []
        for node in frontier:
            spellings = _spellings(cfg, node)
            marks = ",".join("?" * len(spellings))
            found = c.execute(
                f"SELECT * FROM edges WHERE valid_to IS NULL AND (src IN ({marks}) OR dst IN ({marks}))",
                (*spellings, *spellings),
            ).fetchall()
            for d in _identity_rows(found, spellings):
                if d["id"] in {e["id"] for e in edges}:
                    continue
                edges.append(d)
                for n in (d["src"], d["dst"]):
                    key = identity(n)
                    if key not in seen:
                        seen.add(key)  # the frontier carries identity, not a spelling
                        nxt.append(n)
        frontier = nxt
    ents = {identity(r["name"]): dict(r) for r in c.execute("SELECT * FROM entities").fetchall()}
    c.close()
    # an entry id is also a node worth asking about: what cites it, and what it cites (#85)
    known_ids = {e.id for _p, e in iter_entries(cfg.root)}
    cites = citations(cfg, entity) if entity in known_ids else {"entry": entity, "cites": [],
                                                               "cited_by": []}
    return {"nodes": [ents.get(key, {"name": key}) for key in sorted(seen)], "edges": edges,
            "entries": project_entries(cfg, entity, include_pruned=include_pruned),
            "citations": cites}


def materialize(cfg: Config) -> int:
    """Write entities/<slug>.md hubs so the graph is browsable in Obsidian too."""
    c = connect(cfg.db_path)
    ents = c.execute("SELECT * FROM entities ORDER BY name").fetchall()
    cfg.entities_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for e in ents:
        lines = ["---", f"entity: {e['name']}", f"kind: {e['kind']}", "tags: [entity]", "---", "",
                 f"# {e['name']}", ""]
        for r in c.execute(
            "SELECT * FROM edges WHERE src = ? OR dst = ? ORDER BY valid_from", (e["name"], e["name"])
        ).fetchall():
            arrow = "-->" if r["src"] == e["name"] else "<--"
            other = r["dst"] if r["src"] == e["name"] else r["src"]
            window = f"{r['valid_from'][:10]} → {r['valid_to'][:10] if r['valid_to'] else 'now'}"
            lines.append(f"- {arrow} `{r['predicate']}` [[{other}]] ({window})")
        stem = hub_stem(e["name"], e["key"])
        (cfg.entities_dir / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        count += 1
    c.close()
    return count


def _close_spec(cfg: Config, spec: str, at: str) -> bool:
    """Close the live edge this spec names. Exact display match first, identity match second."""
    try:
        src, predicate, dst = parse_edge_spec(spec)
    except ValueError:
        return False
    c = connect(cfg.db_path)
    try:
        rows = c.execute("SELECT id, src, dst FROM edges WHERE predicate = ? AND valid_to IS NULL",
                         (predicate,)).fetchall()
        target = None
        for r in rows:
            if r["src"] == src and r["dst"] == dst:
                target = r["id"]
                break
        if target is None:
            for r in rows:
                if identity(r["src"]) == identity(src) and identity(r["dst"]) == identity(dst):
                    target = r["id"]
                    break
        if target is None:
            return False
        with c:
            c.execute("UPDATE edges SET valid_to = ? WHERE id = ? AND valid_to IS NULL", (at, target))
            c.execute("INSERT INTO events(at, kind, detail) VALUES(?,?,?)",
                      (at, "invalidate", json.dumps({"edge": target}, ensure_ascii=False)))
        return True
    finally:
        c.close()


def rebuild(cfg: Config) -> dict:
    """Fold the graph into the markdown and rebuild the graph from it.

    The db is derived: whatever the entries say is what the graph becomes. Edges that exist only
    in the db are written into the entry that asserted them first, so a rebuild neither loses a
    belief nor resurrects a closed one. The entry's `created` is the window's start, so a rebuilt
    timeline is the timeline that was there before - not a fresh set of "now".

    Returns what happened: how many edges were asserted, how many windows were closed, how many
    db-only edges had no entry to live in.
    """
    entries = {e.id: e for _p, e in iter_entries(cfg.root)}
    c = connect(cfg.db_path)
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM edges ORDER BY id").fetchall()]
    finally:
        c.close()

    orphans = 0
    folded = 0
    # Which live edge replaced which closed one? The db knows the windows: a window that closed at
    # T and another of the same src+predicate that opened at T are the same correction. That link
    # is what lets a replay date the replacement to the moment the old belief died.
    opened_at: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        if not row.get("valid_to"):
            opened_at.setdefault((row["src"], row["predicate"], row["valid_from"]), row)
    for row in rows:
        # the carrier is the entry that asserted the edge (`src`), or the one the row claims — a
        # `note_id` alone hides an edge whose caller wrote prose there (#96)
        candidates = carrier_entries(entries, row["src"], row.get("note_id") or "")
        if not candidates:
            # asserted with no entry behind it: there is nowhere in the markdown to put it
            orphans += 1
            continue
        spec = format_edge_spec(row["src"], row["predicate"], row["dst"])
        entry = candidates[0]
        if not any(entry_records_edge(e, spec) for e in candidates):
            record_edge(cfg, entry.id, spec)
            folded += 1
            # The plan below is built from `entries`, which was read BEFORE this fold. Without
            # refreshing it the fold was invisible for a pass: `folded=1` and the cleared db stayed
            # empty, so the recovered belief only reappeared on a SECOND rebuild (#122).
            path = find_entry(cfg.root, entry.id)
            if path is not None:
                entries[entry.id] = read_entry(path)
        if row["valid_to"]:
            replacement = ""
            cand = opened_at.get((row["src"], row["predicate"], row["valid_to"]))
            if cand is not None:
                replacement = format_edge_spec(cand["src"], cand["predicate"], cand["dst"])
            record_closure(cfg, entry.id, row["src"], row["predicate"], row["dst"], row["valid_to"],
                           replacement=replacement)

    c = connect(cfg.db_path)
    try:
        with c:
            c.execute("DELETE FROM edges")
            c.execute("DELETE FROM entities")
            c.execute("DELETE FROM events")
    finally:
        c.close()

    # A closure that names a replacement says the replacement was asserted at the instant the old
    # window closed. Date it there - not at the entry's creation - or the replayed timeline is a
    # different timeline.
    replacement_start: dict[tuple[str, str, str], str] = {}
    for e in entries.values():
        for closure in e.edge_closures:
            rep = closure.get("replacement")
            if not rep:
                continue
            try:
                s, p, d = parse_edge_spec(rep)
            except ValueError:
                continue
            replacement_start[edge_key(s, p, d)] = closure.get("at", "")

    # asserted in the order the windows open, so the edge ids come out the same as they did. A
    # replacement always follows the edge it replaced: they can share a second, and the original
    # order was old-then-new.
    #
    # A malformed or out-of-vocabulary spec is a broken claim, not a reason to lose the memory:
    # the edge is skipped and counted, the entry is indexed either way. What gets reported has to
    # be actionable — the ENTRY and the SPEC, not just the offending predicate, because the
    # markdown is the truth and a bare predicate name tells nobody which file to edit (#46).
    skipped: list[str] = []
    plan = []
    for e in sorted(entries.values(), key=lambda e: (e.created, e.id)):
        for spec in e.edges:
            try:
                src, predicate, dst = parse_edge_spec(spec)
            except ValueError as exc:
                skipped.append(f"{e.id}: {spec!r} is not a src:predicate:dst spec ({exc})")
                continue
            if predicate not in cfg.predicates:
                skipped.append(f"{e.id}: {spec!r} (predicate {predicate!r} is not in the "
                               f"vocabulary; add it to {cfg.config_path})")
                continue
            key = edge_key(src, predicate, dst)
            at = replacement_start.get(key) or e.created
            plan.append((at, key in replacement_start, e.created, e.id, src, predicate, dst, e.id))
    plan.sort(key=lambda p: (p[0], p[1], p[2], p[3]))

    asserted = 0
    for at, _is_replacement, _created, _eid, src, predicate, dst, note_id in plan:
        try:
            add_edge(cfg, src, predicate, dst, note_id=note_id, at=at,
                     allow_new_predicate=False, record=False)
            asserted += 1
        except ValueError:
            continue

    closed = 0
    for e in entries.values():
        for closure in e.edge_closures:
            if _close_spec(cfg, closure.get("spec", ""), closure.get("at", "")):
                closed += 1
    return {"edges": asserted, "closed": closed, "folded": folded, "orphans": orphans,
            "skipped": skipped}
