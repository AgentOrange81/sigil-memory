"""`sigil repair` — fix imported notes whose summary still carries its YAML quotes (#58, from #54).

The importer read frontmatter line-by-line, so a summary written as a single-quoted YAML scalar arrived
with its quotes attached and cut off at the wrap. The parser is fixed now, but a repaired parser does not
repair a vault: 88 notes on this box still carry a mangled handle, and until they are rewritten every
ranking feature silently works on the wrong text.

Two sources, in this order, because neither alone covers the corpus:

1. **The fabric origin, by frontmatter `id`** (not by filename — fabric names files by slug and suffix,
   while `id:` in the frontmatter is the id sigil kept). 74 of 88 here.
2. **The entry's own body**, whose first H1 line is a copy of the summary the import duplicated. 14 here.

Nothing is deleted, nothing is renamed: the file keeps its path, and only the `summary` line plus the
verbatim copies of the old summary in the body are replaced. A note whose source cannot be found is
reported and left exactly as it is — a repair that guesses is worse than a note that stays wrong until a
human looks at it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .importers.fabric import _split_frontmatter
from .store import Entry, format_edge_spec, iter_entries

QUOTES = ("'", '"')


@dataclass
class Repair:
    id: str
    old: str
    new: str = ""
    source: str = ""        # "fabric:<file>" or "body"
    reason: str = ""        # why it could not be repaired
    path: Path | None = None


@dataclass
class Result:
    repairs: list[Repair] = field(default_factory=list)
    applied: int = 0
    unresolved: int = 0


def _fabric_index(src: Path) -> dict[str, Path]:
    """id -> path, from the frontmatter `id:` field. Filenames are slugs and cannot be trusted."""
    index: dict[str, Path] = {}
    if not src.is_dir():
        return index
    for path in src.glob("*.md"):
        try:
            fm, _body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not fm:
            continue
        fid = str(fm.get("id") or "").strip()
        if fid:
            index.setdefault(fid, path)
    return index


def _unquote(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and text[0] in QUOTES and text[-1] == text[0]:
        text = text[1:-1]
        if text[:1] in QUOTES:      # the old parser also left a stray opening quote inside
            text = text[1:]
    elif text[:1] in QUOTES:
        text = text[1:]
    return text.strip()


def _from_body(entry: Entry) -> str:
    """The longest quoted line in the body that EXTENDS the summary — i.e. carries the cut-off tail.

    The import wrote the summary twice into the body (an H1 and a paragraph), so the body usually only
    repeats what the frontmatter already has. A line proves a recovery only when it is longer and starts
    where the summary starts; anything else is the truncation case, where the missing text is unknowable
    and a stripped quote would be a guess dressed as a repair.
    """
    base = _unquote(entry.summary)
    if not base:
        return ""
    head = base[:30]
    best = ""
    for line in (entry.body or "").splitlines():
        line = line.strip().lstrip("#").strip()
        # a copy of the raw fabric text carries quotes on BOTH ends, but a hand-edited one may carry
        # neither: what matters is the text, not the quoting
        candidate = line.strip("\"'").strip()
        if not candidate:
            continue
        if candidate.startswith(head) and len(candidate) > len(base) and len(candidate) > len(best):
            best = candidate
    return best


#: A summary that ends this way is complete text that was merely quoted. One that ends mid-word or on a
#: comma is truncated by the wrap, and its missing tail is unknowable from the vault.
_TERMINALS = (".", "!", "?", ")", "`", "…")


def _quote_only(entry: Entry) -> str:
    """The text is whole; only the quotes are wrong. Safe only when the value ends like a sentence."""
    base = _unquote(entry.summary)
    if base and base != entry.summary.lstrip("\"'").strip() and base.endswith(_TERMINALS):
        return base
    return ""


def find(vault: str | Path, source: str | Path = "~/fabric") -> Result:
    """Work out what each quoted summary should say. Nothing is written."""
    src = Path(source).expanduser()
    fabric = _fabric_index(src)
    result = Result()
    for path, entry in iter_entries(vault):
        if (entry.summary or "")[:1] not in QUOTES:
            continue
        rep = Repair(id=entry.id, old=entry.summary, path=Path(path))
        origin = fabric.get(entry.id)
        if origin is not None:
            fm, _body = _split_frontmatter(origin.read_text(encoding="utf-8", errors="replace"))
            candidate = _unquote(str((fm or {}).get("summary") or ""))
            if candidate:
                rep.new, rep.source = candidate, f"fabric:{origin.name}"
        if not rep.new:
            candidate = _from_body(entry)
            if candidate:
                rep.new, rep.source = candidate, "body"
        if not rep.new:
            candidate = _quote_only(entry)
            if candidate:
                rep.new, rep.source = candidate, "quotes-only"
        if not rep.new:
            rep.reason = "no source: not in the fabric origin and the body does not repeat the summary"
            result.unresolved += 1
        elif rep.new[:1] in QUOTES:
            rep.reason = "recovered value still starts with a quote"
            result.unresolved += 1
        result.repairs.append(rep)
    return result


def apply(vault: str | Path, result: Result) -> int:
    """Rewrite the notes in place: same path, `summary:` line and its verbatim copies in the body."""
    from .store import _scalar  # the same quoting the writer uses, so the file stays canonical

    written = 0
    for rep in result.repairs:
        if rep.reason or not rep.path or not rep.new:
            continue
        text = rep.path.read_text(encoding="utf-8", errors="replace")
        head, sep, body = text.partition("\n---")   # frontmatter ends at the closing fence
        if not sep:
            rep.reason = "no frontmatter fence — not a note this tool understands"
            continue
        old_line = f"summary: {_scalar(rep.old)}"
        if old_line in head:
            head = head.replace(old_line, f"summary: {_scalar(rep.new)}", 1)
        else:
            rep.reason = "summary line not found in the frontmatter"
            continue
        body = body.replace(rep.old, rep.new)
        rep.path.write_text(head + sep + body, encoding="utf-8")
        written += 1
    result.applied = written
    return written


def reindex(vault: str | Path) -> None:
    """The same recovery `sigil reindex` performs — index AND graph.

    Indexing alone leaves the graph table exactly as it was, so a repair that just rewrote the
    markdown left an index that disagreed with it: `audit` kept reporting the edges the pass had
    re-homed, because the row's `note_id` was still the prose it had always been (#96).
    """
    from . import config as config_mod
    from . import graph as graph_mod
    from . import index as index_mod

    cfg = config_mod.load(vault, create=True)
    index_mod.reindex(cfg.root, cfg.db_path)
    graph_mod.rebuild(cfg)
    graph_mod.materialize(cfg)


# --------------------------------------------------------------------------------------------------
# The edges the index holds and the markdown does not (#94)
#
# `add_edge` refuses a carrier that names no entry now, but a vault written before that rule holds
# edges recorded nowhere: `rm .sigil/index.db` + `reindex` deletes them. Each of them is recoverable
# from its own `src`, because the caller asserted the edge against an entry and used that entry's id
# as the entity name. `note_id` is only a fallback — it is exactly the field a bad caller filled with
# prose, which is how the edges got here.


@dataclass
class EdgeRepair:
    edge: int
    spec: str
    carrier: str = ""        # the entry id that will carry the spec
    reason: str = ""         # why no entry can carry it


@dataclass
class EdgeResult:
    edges: list[EdgeRepair] = field(default_factory=list)
    rehomed: int = 0
    unresolved: int = 0


def find_edges(vault: str | Path) -> EdgeResult:
    """Live edges no entry's markdown records, and the entry that should carry each. Writes nothing."""
    from . import config as config_mod
    from . import graph as graph_mod
    from . import index as index_mod

    cfg = config_mod.load(vault, create=True)
    entries = {e.id: e for _p, e in iter_entries(cfg.root)}
    result = EdgeResult()
    c = index_mod.connect(cfg.db_path)
    try:
        rows = c.execute("SELECT id, src, predicate, dst, note_id FROM edges WHERE valid_to IS NULL "
                         "ORDER BY id").fetchall()
    finally:
        c.close()
    for r in rows:
        spec = format_edge_spec(r["src"], r["predicate"], r["dst"])
        carriers = [c for c in (r["src"], r["note_id"] or "") if c in entries]
        # recorded when EITHER candidate's entry says so: the `src` is who asserted it, the
        # `note_id` is who the row claims, and a rebuild replays any entry that holds the spec
        if any(graph_mod.entry_records_edge(entries[c], spec) for c in carriers):
            continue
        rep = EdgeRepair(edge=r["id"], spec=spec, carrier=carriers[0] if carriers else "")
        if not rep.carrier:
            rep.reason = f"no entry named {r['src']!r} and none named {r['note_id'] or ''!r}"
            result.unresolved += 1
        result.edges.append(rep)
    return result


def apply_edges(vault: str | Path, result: EdgeResult) -> int:
    """Write each spec into its carrier's frontmatter. Same path, no rename, no new id."""
    from . import config as config_mod
    from . import graph as graph_mod

    cfg = config_mod.load(vault, create=True)
    written = 0
    for rep in result.edges:
        if rep.reason or not rep.carrier:
            continue
        if graph_mod.record_edge(cfg, rep.carrier, rep.spec):
            written += 1
    result.rehomed = written
    return written
