"""sigil command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import askfill as askfill_mod
from . import brief as brief_mod
from . import config as config_mod
from . import embed as embed_mod
from . import evidence as evidence_mod
from . import export as export_mod
from . import graph as graph_mod
from . import hook as hook_mod
from . import index as index_mod
from . import promote as promote_mod
from . import repair as repair_mod
from .importers import fabric as fabric_mod
from .store import (
    Entry,
    SummaryNotProse,
    SummaryTooLong,
    check_summary,
    find_entry,
    format_edge_spec,
    iter_entries,
    looks_like_blob,
    looks_like_transcript,
    parse_edge_spec,
    write_entry,
)

#: The on-box tier this box happens to serve. Overridable per call; nothing here assumes it exists.
DEFAULT_CHAT_URL = "http://127.0.0.1:8081/v1/chat/completions"
DEFAULT_CHAT_MODEL = "qwen3.8-flash-next-iq3_xxs"


def _read_stdin() -> str:
    """Read piped input without exploding when stdin is a closed/captured handle."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def _cfg(args):
    cfg = config_mod.load(args.vault, create=False)
    # a db from an older schema is derived, not sacred: rebuild from the markdown rather than migrate
    # rows whose display names no longer resolve (#37)
    note = index_mod.refresh_if_stale(cfg)
    if note:
        print(note, file=sys.stderr)
    return cfg


class _EndpointTimeoutAction(argparse.Action):
    """`--endpoint-timeout`: a positive whole number of seconds, refused loudly.

    argparse's own `type=int` turns a bad value into a usage error that names neither the setting
    nor the rule, and it swallows the exception it is handed. This action raises the ValueError
    the setting deserves — its message names `llm.timeout`, the key the same value lives under in
    `config.toml` — and `main` turns that into a clean exit instead of a traceback.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            seconds = int(str(values))
        except (TypeError, ValueError):
            raise ValueError(f"llm.timeout must be a whole number of seconds, got {values!r}") from None
        if seconds <= 0:
            raise ValueError(f"llm.timeout must be greater than zero, got {seconds!r}")
        setattr(namespace, self.dest, seconds)


def _provider(args, cfg):
    if args.fake:
        return promote_mod.FakeProvider(json.loads(Path(args.fake).read_text()))
    timeout = getattr(args, "endpoint_timeout", None) or cfg.llm_timeout
    return promote_mod.HttpProvider(cfg.llm_base_url, args.model or cfg.llm_model, cfg.llm_api_key,
                                    timeout=timeout)


def cmd_init(args) -> int:
    cfg = config_mod.load(args.vault, create=True)
    index_mod.connect(cfg.db_path).close()
    print(f"initialised vault at {cfg.root}")
    return 0


def cmd_capture(args) -> int:
    cfg = _cfg(args)
    text = _read_stdin().strip() if not args.text else args.text
    if not text:
        print("nothing to capture (pass text or pipe stdin)", file=sys.stderr)
        return 2
    e = promote_mod.record_capture(cfg, text, agent=args.agent, project=args.project,
                                   session=args.session, ask=args.ask, source="capture")
    index_mod.reindex(cfg.root, cfg.db_path)
    print(e.id)
    return 0


def cmd_write(args) -> int:
    cfg = _cfg(args)
    body = _read_stdin().strip() if args.body else args.body
    specs = []
    order = []
    broken = 0
    for spec in args.edge or []:
        try:
            src, predicate, dst = parse_edge_spec(spec)
        except ValueError:
            # a malformed spec is a broken claim, not a reason to lose the memory: the entry is
            # written, the claim is skipped, and the reindex reports it.
            print(f"--edge wants src:predicate:dst, got {spec!r}", file=sys.stderr)
            broken += 1
            order.append(spec)
            continue
        specs.append((spec, src, predicate, dst))
        order.append(spec)
    try:
        check_summary(args.summary, allow_long=getattr(args, "allow_long_summary", False),
                      allow_blob=getattr(args, "allow_summary_blob", False))
    except (SummaryTooLong, SummaryNotProse) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    e = Entry(kind=args.kind, state=args.state, agent=args.agent, project=args.project,
              session=args.session, summary=args.summary, evidence=args.evidence or "",
              ask=args.ask or "", importance=args.importance, body=body, source="cli",
              # classified here for the same reason the MCP writer does it (#128): the field is
              # derived from the evidence text, and a writer that leaves it out does not fail —
              # it silently produces an unclassified note that only the backfill script can fix
              evidence_kind=evidence_mod.classify(args.evidence or ""),
              supersedes=list(args.supersedes or []), edges=order)
    # The entry is written BEFORE the edges are asserted (docs/USING.md): a bad predicate must
    # not cost the memory. The write then fails and the entry is left on disk but unindexed;
    # `sigil reindex` puts it back, skipping the broken claim and saying so.
    p = write_entry(cfg.root, e)
    failed = 0
    for _spec, src, predicate, dst in specs:
        # the entry's `created` is the window's start, so a rebuild from the markdown reproduces
        # the same window instead of stamping it `now`
        try:
            graph_mod.add_edge(cfg, src, predicate, dst, note_id=e.id, at=e.created,
                               allow_new_predicate=args.allow_new)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            failed += 1
    if failed or broken:
        # the write failed: the index is not refreshed, so the entry stays unindexed until a
        # reindex runs. The markdown is the truth; the index is disposable.
        return 2
    # the graph follows --supersedes (#20): the old belief's window closes at the instant the
    # correction was written, and the closure is recorded in the markdown so a rebuild replays it.
    closed = graph_mod.close_superseded(cfg, e.supersedes, e) if e.supersedes else []
    for row in closed:
        print(f"closed edge {row['edge']} {row['spec']} (superseded {row['superseded']})",
              file=sys.stderr)
    index_mod.reindex(cfg.root, cfg.db_path)
    graph_mod.materialize(cfg)
    print(p)
    return 0


def _dedupe_proposals(proposals):
    """Kept as the CLI's name for the shared rule; the implementation lives in promote (#2)."""
    return promote_mod.dedupe_proposals(proposals)


def cmd_reflect(args) -> int:
    """A reflection pass over one transcript, chunked on turn boundaries when it is long (#2).

    The transcript is loaded by shape (Hermes JSONL or markdown), split so no message is ever cut in
    half, and each chunk is reflected separately. Failures are loud and name the endpoint: a pass that
    reached nothing must not exit 0.
    """
    cfg = _cfg(args)
    if getattr(args, "session_db", ""):
        # a live Hermes session lives in state.db, not in sessions/<id>.jsonl (#101)
        try:
            transcript = promote_mod.read_session_db(args.session_db, args.session or "",
                                                     include_tools=args.include_tools)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    else:
        source = args.transcript if args.transcript else _read_stdin()
        try:
            transcript = promote_mod.load_transcript(source)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if promote_mod.LAST_LOAD_STATS.malformed:
        print(f"{promote_mod.LAST_LOAD_STATS.malformed} malformed lines skipped", file=sys.stderr)
    existing = [e.summary for _p, e in iter_entries(cfg.root) if e.state == "promoted"]
    chunks = promote_mod.chunk_transcript(transcript, args.max_chars)
    provider = _provider(args, cfg)
    proposals = []
    try:
        for number, chunk in enumerate(chunks, 1):
            got = promote_mod.reflect(chunk, cfg, provider, existing)
            if len(chunks) > 1:
                # the dry-run contract is ONE json document on stdout, so the breakdown goes to
                # stderr there; a normal run keeps its breakdown where the operator reads it
                print(f"chunk {number}/{len(chunks)}: {len(got)} proposals",
                      file=sys.stderr if args.dry_run else sys.stdout)
            proposals.extend(got)
    except Exception as exc:  # a failed pass is a non-zero exit with the reason, never a traceback
        print(f"reflect failed at {cfg.llm_base_url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    proposals = _dedupe_proposals(proposals)
    if args.dry_run:
        print(json.dumps([p.__dict__ for p in proposals], indent=2, ensure_ascii=False))
        return 0
    report: dict = {}
    written = promote_mod.apply(proposals, cfg, agent=args.agent, project=args.project,
                                session=args.session, source=args.transcript or "stdin",
                                report=report)
    for spec in report.get("skipped_edges") or []:
        print(f"skipped edge {spec}: predicate is outside the closed vocabulary", file=sys.stderr)
    graph_mod.materialize(cfg)
    index_mod.reindex(cfg.root, cfg.db_path)
    promoted = sum(1 for e in written if e.state == "promoted")
    print(f"{len(written)} proposals, {promoted} promoted, {len(written) - promoted} left raw")
    return 0


def cmd_import(args) -> int:
    """Bring an external corpus into the vault (currently: Icarus/fabric markdown)."""
    src = Path(args.src_dir)
    if not src.is_dir():
        print(f"not a directory: {src}", file=sys.stderr)
        return 2
    # `--vault` may come before or after the subcommand; the subcommand's copy wins.
    vault = getattr(args, "vault_override", None) or args.vault
    if args.dry_run:
        cfg = config_mod.load_lenient(vault)
        result = fabric_mod.import_fabric(src, cfg.root, dry_run=True,
                                         include_noise=args.include_noise,
                                         promote_threshold=cfg.promote_threshold,
                                         recurse=args.recurse, skip_dirs=args.skip_dirs)
        print(json.dumps(_import_summary(result, src, vault, True), indent=2))
        return 0
    cfg = config_mod.load(vault, create=True)
    result = fabric_mod.import_fabric(src, cfg.root, dry_run=False, include_noise=args.include_noise,
                                      promote_threshold=cfg.promote_threshold,
                                      recurse=args.recurse, skip_dirs=args.skip_dirs)
    index_mod.reindex(cfg.root, cfg.db_path)
    print(json.dumps(_import_summary(result, src, vault, False), indent=2))
    return 0


def _import_summary(result, src, vault, dry_run) -> dict:
    reasons: dict[str, int] = {}
    for _path, reason in result.skipped:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "source": str(src),
        "vault": str(vault),
        "dry_run": dry_run,
        "written": len(result.written),
        "skipped": len(result.skipped),
        "skipped_by_reason": dict(sorted(reasons.items())),
        "directories_ignored": dict(sorted(result.directories_ignored.items())),
        "by_kind": result.by_kind,
        "by_state": result.by_state,
    }


def cmd_search(args) -> int:
    cfg = _cfg(args)
    note = ""
    if getattr(args, "hybrid", False):
        try:
            rows, note = embed_mod.hybrid_search(
                cfg.db_path, args.query, args.embed_url or cfg.embed_url,
                args.embed_model or cfg.embed_model, limit=args.limit, kind=args.kind,
                state=args.state, agent=args.agent, project=args.project,
                since=args.since, until=args.until)
        except ValueError as exc:
            print(f"--since/--until: {exc}", file=sys.stderr)
            return 2
        if note:
            print(note, file=sys.stderr)
    else:
        try:
            rows = index_mod.search(cfg.db_path, args.query, kind=args.kind, state=args.state,
                                    agent=args.agent, project=args.project, limit=args.limit,
                                    since=args.since, until=args.until)
        except ValueError as exc:
            print(f"--since/--until: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0 if rows else 1
    if not rows:
        print("no matches")
        return 1
    for i, r in enumerate(rows, 1):
        print(f"{i}. [{r['id']}] {r['state']:<8} {r['kind']:<10} {r['summary'][:90]}")
        snip = " ".join((r["snip"] or "").split())
        if snip:
            print(f"   {snip[:200]}")
    return 0


def cmd_show(args) -> int:
    cfg = _cfg(args)
    p = find_entry(cfg.root, args.id)
    if not p:
        print(f"no entry {args.id}", file=sys.stderr)
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_edge(args) -> int:
    cfg = _cfg(args)
    if not all(part.strip() for part in (args.src, args.predicate, args.dst)):
        # `quipu edge "" contradicts x` asserted a live edge with an empty subject (measured: it
        # created one, and only a rebuild dropped it). The positional names ARE the spec; an empty
        # one is not a name, and `parse_edge_spec` refuses that shape everywhere else (#107).
        print("edge needs a non-empty src, predicate and dst", file=sys.stderr)
        return 2
    if not args.note:
        # an edge in no entry is an edge the markdown does not know about, and `rm index.db`
        # would take it with the db. Say so, loudly, on stderr.
        print("warning: no --note: this edge is in no entry, so a reindex will drop it",
              file=sys.stderr)
    try:
        eid = graph_mod.add_edge(cfg, args.src, args.predicate, args.dst, note_id=args.note or "",
                                 confidence=args.confidence, allow_new_predicate=args.allow_new)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    graph_mod.materialize(cfg)
    print(eid)
    return 0


def cmd_invalidate(args) -> int:
    cfg = _cfg(args)
    graph_mod.invalidate(cfg, args.edge)
    graph_mod.materialize(cfg)
    print(f"closed edge {args.edge}")
    return 0


def cmd_contradict(args) -> int:
    cfg = _cfg(args)
    try:
        eid = graph_mod.contradict(cfg, args.edge, args.src, args.predicate, args.dst,
                                   note_id=args.note or "")
    except ValueError as exc:
        # a refused replacement closes nothing: the old belief stays live and the db is unchanged
        print(str(exc), file=sys.stderr)
        return 2
    graph_mod.materialize(cfg)
    print(eid)
    return 0


def cmd_timeline(args) -> int:
    cfg = _cfg(args)
    rows = graph_mod.timeline(cfg, args.entity, at=args.at)
    for r in rows:
        end = r["valid_to"] or "now"
        print(f"{r['id']:>4} {r['valid_from'][:19]} → {end[:19]}  {r['src']} -{r['predicate']}-> {r['dst']}")
    hub = graph_mod.project_entries(cfg, args.entity,
                                    include_pruned=getattr(args, "all_states", False))
    for r in hub:
        # the project-hub view (#85): dated by the entry's own creation, which is the only instant
        # the markdown records
        print(f"     {(r['created'] or '')[:19]}  entry {r['id']}  {r['summary'][:88]}")
    return 0 if (rows or hub) else 1


def cmd_neighbors(args) -> int:
    cfg = _cfg(args)
    out = graph_mod.neighbors(cfg, args.entity, depth=args.depth,
                              include_pruned=getattr(args, "all_states", False))
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for n in out["nodes"]:
        print(f"node  {n['name']}")
    for e in out["edges"]:
        print(f"edge  {e['src']} -{e['predicate']}-> {e['dst']}")
    for r in out.get("entries") or []:
        # the project-hub view (#85): the tag is the assertion, so these are entries, not edges
        print(f"entry {r['id']}  {r['state']:<8} {r['kind']:<10} {r['summary'][:88]}")
    cit = out.get("citations") or {}
    for i in cit.get("cites") or []:
        print(f"cites {i}")
    for i in cit.get("cited_by") or []:
        print(f"cited-by {i}")
    return 0


def cmd_export(args) -> int:
    cfg = _cfg(args)
    out = Path(args.out)
    if args.which == "sft":
        kinds = set(args.evidence_kind.split(",")) if getattr(args, "evidence_kind", "") else None
        n = export_mod.export_sft(cfg, out, system=args.system, evidence_kinds=kinds,
                                  min_evidence_bytes=getattr(args, "min_evidence_bytes", 0))
        print(f"{n} sft examples -> {out}")
    elif args.which == "dpo":
        n = export_mod.export_preferences(cfg, out)
        print(f"{n} preference pairs -> {out}")
    elif args.which == "graph":
        r = export_mod.export_graph(cfg, out)
        print(f"{r['nodes']} nodes, {r['edges']} edges -> {out}")
    return 0


def cmd_reindex(args) -> int:
    """Rebuild the index AND the graph. The db is derived from the markdown, so this is the
    honest recovery for a stale schema, a lost db, or a vault whose graph moved into notes."""
    cfg = _cfg(args)
    n = index_mod.reindex(cfg.root, cfg.db_path)
    report = graph_mod.rebuild(cfg)
    graph_mod.materialize(cfg)
    summary = f"indexed {n} entries, rebuilt {report['edges']} edges ({report['closed']} closed)"
    skipped = list(dict.fromkeys(report.get("skipped") or []))
    if skipped:
        # a broken edge spec is reported, not fatal: the entry is indexed and the memory is kept.
        # Counted in the summary line, then one line per skip that names the ENTRY and the SPEC —
        # a bare predicate name leaves the operator with nowhere to go, and the markdown is the truth.
        summary += f", {len(skipped)} edge{'' if len(skipped) == 1 else 's'} skipped"
    print(summary)
    for reason in skipped:
        print(f"  skipped: {reason}")
    if report["folded"]:
        print(f"folded {report['folded']} db-only edges into the markdown")
    if report["orphans"]:
        print(f"warning: {report['orphans']} edges were in no entry and were dropped",
              file=sys.stderr)
    return 0


def cmd_stats(args) -> int:
    cfg = _cfg(args)
    report = export_mod.dataset_report(cfg)
    print(json.dumps(report, indent=2))
    return 0


def _index_fields_lost(cfg, entries) -> int:
    """How many entries carry a derived field in the markdown that the index row does not.

    Only counts fields the entry actually has, so an old vault (no `model:` anywhere) reads as clean.
    """
    import sqlite3

    if not cfg.db_path.exists():
        return 0
    want_kind = sum(1 for e in entries.values() if e.evidence_kind)
    want_model = sum(1 for e in entries.values() if e.model)
    try:
        c = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        try:
            have_kind = c.execute(
                "SELECT count(*) FROM entries WHERE evidence_kind IS NOT NULL AND evidence_kind != ''"
            ).fetchone()[0]
            have_model = c.execute(
                "SELECT count(*) FROM entries WHERE model IS NOT NULL AND model != ''"
            ).fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0
    return max(0, want_kind - have_kind) + max(0, want_model - have_model)


def cmd_audit(args) -> int:
    """Cheap health check: unreachable links, missing evidence, dangling supersedes."""
    report = audit_report(_cfg(args))
    print(json.dumps(report, indent=2))
    return 0 if not report["problems"] else 1


def audit_report(cfg) -> dict:
    """The audit's findings as a dict, so callers other than the CLI can act on them.

    `sigil-capture` asks this on every timer tick and reindexes when the answer says the derived copy
    lost fields (#128) — the same question the CLI prints, without spawning the CLI to print it.
    """
    entries = {e.id: e for _p, e in iter_entries(cfg.root)}
    problems = []
    # A stale writer is invisible until someone reads the column it dropped. Measured: a process
    # holding pre-#109 code rebuilt the live index and blanked evidence_kind and model on all 2064
    # rows while the markdown kept both (#128). The audit stands on the markdown, so it can see that.
    lost = _index_fields_lost(cfg, entries)
    if lost:
        problems.append(f"{lost} entries: the index lost fields the markdown still has "
                        f"(a client running older code rebuilt it — rerun `sigil reindex`)")
    for e in entries.values():
        if e.state == "promoted" and not e.evidence:
            problems.append(f"{e.id}: promoted without evidence")
        if e.state == "promoted" and looks_like_transcript(e.summary):
            # a trusted entry whose summary is a pasted tool transcript (#30): an agent reads it
            # as a statement. Filed as a class so a vault can be triaged without reading it.
            problems.append(f"{e.id}: suspicious_summary")
        for old in e.supersedes:
            if old not in entries:
                problems.append(f"{e.id}: supersedes missing entry {old}")
        if not e.summary:
            problems.append(f"{e.id}: empty summary")
        if looks_like_blob(e.summary):
            # a blob where the handle belongs (#77): imported before the write boundary refused the
            # shape. `sigil reindex` keeps it searchable, so the audit is how it stays findable.
            problems.append(f"{e.id}: blob summary (re-import or rewrite as one sentence)")
        for spec in e.edges:
            # the markdown is what a rebuild replays, so the audit validates IT, not only the rows
            # derived from it: a spec the replay refuses never reaches a row, and the old check
            # therefore called the vault clean while three assertions were unqueryable (#98)
            try:
                _src, predicate, _dst = parse_edge_spec(spec)
            except ValueError as exc:
                problems.append(f"{e.id}: malformed edge spec {spec!r} ({exc})")
                continue
            if predicate not in cfg.predicates:
                problems.append(f"{e.id}: edge predicate {predicate!r} is outside the closed "
                                f"vocabulary ({spec!r}; add it to {cfg.config_path})")
        if e.summary[:1] in ("'", '"'):
            # an imported-and-quoted summary (#54): the importer keeps the markdown verbatim, so a
            # summary written as a YAML quoted scalar arrived with its quotes and often truncated at the
            # wrap. The parser is fixed, but the existing notes are not repaired by that - the audit is
            # how the damage stays visible instead of silently ranking 88 entries on a mangled handle.
            problems.append(f"{e.id}: summary starts with a quote character (re-import or edit)")
    # the graph must live in the markdown (#24): an edge no entry records is data that
    # `rm .sigil/index.db` would take with it.
    c = index_mod.connect(cfg.db_path)
    rows = c.execute("SELECT id, src, predicate, dst, note_id, valid_to FROM edges").fetchall()
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    # a superseded entry whose belief is still live is the exact failure #20 produced: two live
    # windows for one fact, and the stale claim looks as current as the correction. The write path
    # closes it now; this line is what says so out loud when a vault was written by an older one.
    for r in rows:
        if r["predicate"] not in cfg.predicates:
            # a predicate outside the closed vocabulary is how a graph rots into 400 spellings of
            # "uses" (#85): report it once per edge, live or closed, because the markdown keeps both
            problems.append(f"edge {r['id']} ({r['src']} -{r['predicate']}-> {r['dst']}): predicate "
                            f"{r['predicate']!r} is outside the closed vocabulary")
    live_notes = {r["note_id"] for r in rows if r["valid_to"] is None and r["note_id"]}
    for e in entries.values():
        for old in e.supersedes:
            if old in live_notes:
                problems.append(f"{e.id}: supersedes {old} but its edge is still live")
    unrecorded = []
    for r in rows:
        # `src` first: that is the entry that asserted the edge, and `note_id` is the column a bad
        # caller filled with prose. Asking one of them let a repaired vault report itself broken (#96)
        candidates = graph_mod.carrier_entries(entries, r["src"], r["note_id"] or "")
        spec = format_edge_spec(r["src"], r["predicate"], r["dst"])
        if not any(graph_mod.entry_records_edge(e, spec) for e in candidates):
            unrecorded.append({"edge": r["id"], "spec": spec, "note_id": r["note_id"]})
    return {"entries": len(entries), "problems": problems,
            "schema_version": version, "edges_not_in_markdown": unrecorded}


def cmd_ask_backfill(args) -> int:
    """Write the question each entry answers (#27). Dry run unless --apply, sample first."""
    cfg = _cfg(args)
    # default to the free on-box tier; any OpenAI-compatible endpoint works (--url/--model/--api-key)
    url = args.url or DEFAULT_CHAT_URL
    model = args.model or DEFAULT_CHAT_MODEL
    todo = askfill_mod.candidates(cfg.root, state=args.state, limit=args.limit)
    if not todo:
        print(json.dumps({"candidates": 0, "note": "every entry already has an ask"}, indent=2))
        return 0
    if args.count:
        print(json.dumps({"candidates": len(todo)}, indent=2))
        return 0

    def progress(index, prop):
        if args.verbose:
            print(f"  [{index}/{len(todo)}] {prop.id} -> {prop.ask or prop.reason}", file=sys.stderr)

    # a sample must not cost a whole-corpus run: bound it to the sample size
    effective = args.limit or (args.sample if args.sample else 0)
    result = askfill_mod.run(cfg.root, url, model, api_key=args.api_key or "",
                             limit=effective, state=args.state, progress=progress)
    for prop in result.proposals[: max(args.sample, 0) or None]:
        print(f"{prop.id}  {prop.summary[:70]!r}\n  -> {prop.ask or '[' + prop.reason + ']'}")
    if args.sample:
        print(f"\n{len(result.proposals)} proposed, {result.skipped} skipped, {result.failed} failed "
              f"— sample only, nothing written.", file=sys.stderr)
        return 0
    if not args.apply:
        print(json.dumps({"candidates": len(result.proposals), "proposed": len(result.proposals)
                          - result.skipped - result.failed, "skipped": result.skipped,
                          "failed": result.failed, "written": 0,
                          "note": "dry run — re-run with --apply"}, indent=2))
        return 0
    written = askfill_mod.apply(cfg.root, result)
    askfill_mod.reindex(cfg.root)
    print(json.dumps({"candidates": len(result.proposals), "written": written,
                      "skipped": result.skipped, "failed": result.failed}, indent=2))
    return 0 if result.failed == 0 else 1


def _repair_edges(cfg, args) -> int:
    """Re-home every live edge the markdown does not record into the entry that asserted it (#94).

    Reported by edge id, spec and the entry that will carry it; an edge with no entry to carry it is
    reported and counted, never dropped and never given a guessed carrier. Exits non-zero while any
    remain, the same contract `sigil repair` already makes for a summary it cannot recover.
    """
    result = repair_mod.find_edges(cfg.root)
    if not result.edges:
        print(json.dumps({"edges_not_in_markdown": 0, "note": "every live edge is in an entry"},
                         indent=2))
        return 0
    if not args.apply:
        for rep in result.edges:
            where = rep.carrier or f"unresolved: {rep.reason}"
            print(f"edge {rep.edge}  {rep.spec}  -> {where}")
        print(f"\n{len(result.edges)} edges the markdown does not record, {result.unresolved} with "
              f"no entry to carry them. Nothing written — re-run with --apply.", file=sys.stderr)
        return 0
    written = repair_mod.apply_edges(cfg.root, result)
    repair_mod.reindex(cfg.root)
    print(json.dumps({"edges_not_in_markdown": len(result.edges), "rehomed": written,
                      "unresolved": result.unresolved}, indent=2))
    return 0 if result.unresolved == 0 else 1


def cmd_repair(args) -> int:
    """Fix imported notes whose summary still carries its YAML quotes (#58), or re-home the edges
    the index holds and the markdown does not (#94). Dry run by default."""
    cfg = _cfg(args)
    if args.edges:
        return _repair_edges(cfg, args)
    result = repair_mod.find(cfg.root, args.source)
    if not result.repairs:
        print(json.dumps({"quoted_summaries": 0, "note": "nothing to repair"}, indent=2))
        return 0
    if not args.apply:
        for rep in result.repairs:
            state = rep.reason or f"from {rep.source}"
            print(f"{rep.id}  [{state}]\n  old: {rep.old[:110]!r}\n  new: {rep.new[:110]!r}")
        print(f"\n{len(result.repairs)} quoted summaries, {result.unresolved} with no recoverable source. "
              f"Nothing written — re-run with --apply.", file=sys.stderr)
        return 0
    written = repair_mod.apply(cfg.root, result)
    repair_mod.reindex(cfg.root)
    print(json.dumps({"quoted_summaries": len(result.repairs), "rewritten": written,
                      "unresolved": result.unresolved}, indent=2))
    return 0 if result.unresolved == 0 else 1


def cmd_brief(args) -> int:
    """Render the standing answer: promoted entries, one line each (#51). Derived, never an entry."""
    cfg = _cfg(args)
    scope = args.project or args.agent or ""
    text = brief_mod.build((e for _p, e in iter_entries(cfg.root)), project=args.project,
                           agent=args.agent, scope=scope, max_lines=args.limit)
    if args.stdout:
        sys.stdout.write(text)
        return 0
    rel = args.out or brief_mod.default_relpath(scope)
    path = cfg.meta_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    lines = text.count("\n- ") + text.count("\n  - ")
    print(f"{path} ({len(text.encode('utf-8'))} bytes, {lines} lines) · {brief_mod.stamp()}",
          file=sys.stderr)
    print(str(path))
    return 0


def cmd_embed(args) -> int:
    """Build or refresh the vector index for this vault (#5). Derived data: re-run after a reindex."""
    cfg = _cfg(args)
    url = args.embed_url or cfg.embed_url
    if not url:
        print("no embedding endpoint: pass --url or set [embed] url in .sigil/config.toml",
              file=sys.stderr)
        return 2
    model = args.embed_model or cfg.embed_model
    entries = [e for _p, e in iter_entries(cfg.root)]
    wanted = {}
    for e in entries:
        text = embed_mod.entry_text(e.summary, e.ask, e.body)
        if text:
            wanted[e.id] = (embed_mod.text_digest(text), text)
    c = index_mod.connect(cfg.db_path)
    try:
        have = {} if args.force else embed_mod.stored_digests(c, model)
        todo = {eid: text for eid, (digest, text) in wanted.items() if have.get(eid) != digest}
        vectors: dict[str, tuple[str, list[float]]] = {}
        ids = list(todo)
        for start in range(0, len(ids), 64):
            chunk = ids[start:start + 64]
            got = embed_mod.embed_texts(url, model, [todo[i] for i in chunk], batch=args.batch)
            for eid, vec in zip(chunk, got, strict=False):
                vectors[eid] = (wanted[eid][0], vec)
        written = embed_mod.store(c, model, vectors) if vectors else 0
    finally:
        c.close()
    print(f"{written} embedded, {len(wanted) - written} unchanged, {len(entries)} entries, "
          f"model {model}")
    return 0


def cmd_hook(args) -> int:
    """The session-end adapter: one reflection pass, no partial writes, non-zero on every failure (#3).

    A hook that exits 0 after doing nothing is the failure this project exists because of, so the
    only success path is a completed pass; `SIGIL_HOOK_DRY_RUN=1` prints the prompt and writes nothing.
    """
    cfg = _cfg(args)
    dry = args.dry_run or hook_mod.dry_run_requested()
    source = args.transcript
    if getattr(args, "session_db", ""):
        try:
            source = promote_mod.read_session_db(args.session_db, args.session or "",
                                                 include_tools=args.include_tools)
        except ValueError as exc:
            print(f"hook failed: {exc}", file=sys.stderr)
            return 1
    try:
        result = hook_mod.run(cfg, source, _provider(args, cfg),
                              agent=args.agent, project=args.project, session=args.session,
                              timeout=args.timeout, dry_run=dry, max_chars=args.max_chars)
    except hook_mod.HookError as exc:
        print(f"hook failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a hook must never die with a traceback and no exit code
        print(f"hook failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if dry:
        print(result.prompt)
        return 0
    print(result.line())
    for spec in result.skipped_edges:
        print(f"warning: skipped edge {spec}: predicate is outside the closed vocabulary",
              file=sys.stderr)
    if result.proposed == 0:
        # a completed pass that extracted nothing is a legitimate outcome (a thin transcript) but it
        # must not be INVISIBLE: a hook that runs for a week and writes nothing looks exactly like
        # this, and that is the failure this project exists because of
        print("warning: the reflection pass proposed no entries — the transcript may be too thin "
              "or the model may be refusing; nothing was written", file=sys.stderr)
    return 0


def cmd_mcp(args) -> int:
    """Serve the vault over MCP on stdio — tools instead of a subprocess per query."""
    from .mcp import serve
    return serve(_cfg(args).root)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sigil", description="local-first agent memory")
    p.add_argument("--vault", default=".", help="vault root (default: cwd)")
    p.add_argument("--version", action="version", version=f"sigil {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, text: str, fn) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=text)
        sp.set_defaults(fn=fn)
        return sp

    add("init", "create a vault", cmd_init)

    s = add("capture", "store a raw capture (text or stdin)", cmd_capture)
    s.add_argument("text", nargs="?")
    for flag in ("agent", "project", "session", "ask"):
        s.add_argument(f"--{flag}", default="")

    s = add("write", "write a curated entry directly", cmd_write)
    s.add_argument("--kind", default="conclusion")
    s.add_argument("--state", default="promoted")
    s.add_argument("--summary", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--evidence", default="")
    s.add_argument("--ask", default="")
    s.add_argument("--agent", default="")
    s.add_argument("--project", default="")
    s.add_argument("--session", default="")
    s.add_argument("--importance", type=float, default=0.8)
    s.add_argument("--supersedes", action="append", help="entry id this one corrects (repeatable)")
    s.add_argument("--edge", action="append", metavar="SRC:PRED:DST",
                   help="assert a graph edge from this entry (repeatable)")
    s.add_argument("--allow-new", action="store_true")
    s.add_argument("--allow-long-summary", action="store_true",
                   help=f"permit a summary over {280} bytes (you almost certainly want --body)")
    s.add_argument("--allow-summary-blob", action="store_true",
                   help="permit a summary that opens with '{', '[' or a code fence (it will rank "
                        "badly: put the blob in --body and write one sentence here)")

    add("mcp", "serve the vault over MCP (stdio JSON-RPC)", cmd_mcp)

    s = add("import", "import a corpus into the vault", cmd_import)
    s.add_argument("--vault", dest="vault_override", default=None,
                   help="vault root (same as the global --vault)")
    fmt = s.add_subparsers(dest="format", required=True)

    fs = fmt.add_parser("fabric", help="import an Icarus/fabric corpus")
    fs.add_argument("src_dir")
    fs.add_argument("--vault", dest="vault_override", default=argparse.SUPPRESS,
                    help="vault root (same as the global --vault)")
    fs.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    fs.add_argument("--include-noise", action="store_true",
                    help="also import low-value session entries (as pruned)")
    fs.add_argument("--recurse", action="store_true",
                    help="read the whole tree, not just the top level (default: top-level *.md only; "
                         "the report's directories_ignored lists what the default left behind)")
    fs.add_argument("--skip-dir", action="append", metavar="NAME", dest="skip_dirs",
                    help="with --recurse, a subtree to refuse; repeatable "
                         f"(default: {', '.join(sorted(fabric_mod.DEFAULT_SKIP_DIRS))})")

    s = add("reflect", "run the promotion pass over a transcript", cmd_reflect)
    s.add_argument("transcript", nargs="?")
    s.add_argument("--agent", default="")
    s.add_argument("--project", default="")
    s.add_argument("--session", default="")
    s.add_argument("--model", default="")
    s.add_argument("--fake", help="JSON responses file (no LLM call)")
    s.add_argument("--max-chars", type=int, default=24000,
                   help="split the transcript on turn boundaries at about this size "
                        "(0 disables chunking; a single longer turn is still kept whole)")
    s.add_argument("--endpoint-timeout", action=_EndpointTimeoutAction, default=None,
                   help="override the [llm] timeout (seconds) for this request")
    s.add_argument("--session-db", default="",
                   help="read the transcript from a Hermes state.db instead of a file "
                        "(needs --session; live sessions are not written to sessions/*.jsonl)")
    s.add_argument("--include-tools", action="store_true",
                   help="with --session-db: also read tool results, capped at 2000 chars each")
    s.add_argument("--dry-run", action="store_true")

    s = add("hook", "session-end adapter: reflect a transcript, fail loudly", cmd_hook)
    s.add_argument("--transcript", default="-",
                   help="session file (Hermes JSONL or markdown); '-' reads stdin")
    s.add_argument("--agent", default="")
    s.add_argument("--project", default="")
    s.add_argument("--session", default="")
    s.add_argument("--model", default="")
    s.add_argument("--fake", help="JSON responses file (no LLM call)")
    s.add_argument("--timeout", type=float, default=hook_mod.DEFAULT_TIMEOUT,
                   help=f"abort the reflection after N seconds (default {hook_mod.DEFAULT_TIMEOUT:g})")
    s.add_argument("--max-chars", type=int, default=24000,
                   help="split the transcript on turn boundaries at about this size "
                        "(0 disables chunking; a single longer turn is still kept whole)")
    s.add_argument("--endpoint-timeout", action=_EndpointTimeoutAction, default=None,
                   help="override the [llm] timeout (seconds) for each request — "
                        "--timeout is the budget for the whole pass")
    s.add_argument("--session-db", default="",
                   help="read the transcript from a Hermes state.db instead of a file "
                        "(needs --session; live sessions are not written to sessions/*.jsonl)")
    s.add_argument("--include-tools", action="store_true",
                   help="with --session-db: also read tool results, capped at 2000 chars each")
    s.add_argument("--dry-run", action="store_true",
                   help="print the prompt and write nothing (also SIGIL_HOOK_DRY_RUN=1)")

    s = add("search", "full-text search", cmd_search)
    s.add_argument("query")
    for flag in ("kind", "state", "agent", "project"):
        s.add_argument(f"--{flag}")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.add_argument("--since", default=None,
                   help="only entries created on/after this (2026-10-01, or an ISO instant)")
    s.add_argument("--until", default=None,
                   help="only entries created through this DAY, or before an exact instant")
    s.add_argument("--hybrid", action="store_true",
                   help="fuse the lexical ranking with stored embeddings (#5)")
    s.add_argument("--embed-url", default="", help="override [embed] url")
    s.add_argument("--embed-model", default="", help="override [embed] model")

    s = add("ask-backfill", "write the question each entry answers", cmd_ask_backfill)
    s.add_argument("--url", default="", help="OpenAI-compatible /chat/completions endpoint")
    s.add_argument("--model", default="", help="model id at that endpoint")
    s.add_argument("--api-key", default="", help="bearer token (usually not needed for a local tier)")
    s.add_argument("--state", default="promoted", help="only entries in this state ('' for all)")
    s.add_argument("--limit", type=int, default=0, help="stop after N entries (0 = all)")
    s.add_argument("--sample", type=int, default=0, help="print N proposals and write nothing")
    s.add_argument("--count", action="store_true", help="just report how many entries are missing an ask")
    s.add_argument("--apply", action="store_true", help="write the ask lines (default: dry run)")
    s.add_argument("--verbose", action="store_true")

    s = add("repair", "fix imported notes whose summary still carries its YAML quotes", cmd_repair)
    s.add_argument("--source", default="~/fabric", help="where the fabric originals live")
    s.add_argument("--edges", action="store_true",
                   help="instead: re-home the live edges the index holds and the markdown does "
                        "not record (#94)")
    s.add_argument("--apply", action="store_true", help="write the notes (default: dry run)")

    s = add("brief", "write the standing answer: promoted entries, one line each", cmd_brief)
    s.add_argument("--project", default="")
    s.add_argument("--agent", default="")
    s.add_argument("--limit", type=int, default=brief_mod.DEFAULT_LIMIT,
                   help=f"max entries (default {brief_mod.DEFAULT_LIMIT}, 0 = all)")
    s.add_argument("--out", default="", help="path relative to .sigil/ (default .sigil/brief/<scope>.md)")
    s.add_argument("--stdout", action="store_true", help="print instead of writing")

    s = add("embed", "build or refresh the vector index (needs an embeddings endpoint)", cmd_embed)
    s.add_argument("--url", dest="embed_url", default="", help="OpenAI-compatible /v1/embeddings")
    s.add_argument("--model", dest="embed_model", default="", help="model name the endpoint expects")
    s.add_argument("--batch", type=int, default=32)
    s.add_argument("--force", action="store_true", help="re-embed unchanged entries too")

    s = add("show", "print an entry", cmd_show)
    s.add_argument("id")

    s = add("edge", "assert a graph edge", cmd_edge)
    s.add_argument("src")
    s.add_argument("predicate")
    s.add_argument("dst")
    s.add_argument("--note", default="",
                   help="the id of the entry that carries this edge (e.g. 1a2b3c4d) - an entry "
                        "id, not a description; an unknown id is refused")
    s.add_argument("--confidence", type=float, default=1.0)
    s.add_argument("--allow-new", action="store_true")

    s = add("invalidate", "close an edge's validity window", cmd_invalidate)
    s.add_argument("edge", type=int)

    s = add("contradict", "close an edge and assert its replacement", cmd_contradict)
    s.add_argument("edge", type=int)
    s.add_argument("src")
    s.add_argument("predicate")
    s.add_argument("dst")
    s.add_argument("--note", default="",
                   help="the id of the entry that carries the replacement edge (e.g. 1a2b3c4d) - an "
                        "entry id, not a description; an unknown id is refused")

    s = add("timeline", "an entity's edges, optionally as of a date", cmd_timeline)
    s.add_argument("entity")
    s.add_argument("--at", default=None)
    s.add_argument("--all", dest="all_states", action="store_true",
                   help="include pruned entries in the project-hub view")

    s = add("neighbors", "graph neighbourhood", cmd_neighbors)
    s.add_argument("entity")
    s.add_argument("--depth", type=int, default=1)
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", dest="all_states", action="store_true",
                   help="include pruned entries in the project-hub view (audit work, not recall)")

    s = add("export", "write a dataset", cmd_export)
    s.add_argument("which", choices=("sft", "dpo", "graph"))
    s.add_argument("--out", required=True)
    s.add_argument("--system", default=None)
    s.add_argument("--evidence-kind", default="",
                   help="sft only: keep these evidence kinds (comma separated: command,path,url,claim)")
    s.add_argument("--min-evidence-bytes", type=int, default=0,
                   help="sft only: drop entries whose evidence is shorter than this")

    add("reindex", "rebuild the sqlite index", cmd_reindex)
    add("stats", "dataset + graph report", cmd_stats)
    add("audit", "find corpus problems", cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except ValueError as exc:  # a flag value the setting cannot use: name the setting, exit 2
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except config_mod.VaultNotFound as exc:  # a wrong --vault: name the path, create nothing (#141)
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
