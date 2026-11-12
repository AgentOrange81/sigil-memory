"""MCP server over stdio — JSON-RPC 2.0, one object per line, no dependencies.

An agent that shells out to `sigil search` pays a process per call and has to parse a table; worse, it
has to be told to look at all. Exposing the vault as MCP tools makes recall something the agent can do
the way it does anything else, and it is the difference between a memory system that is available and one
that is used.

This is deliberately hand-rolled: the package's promise is zero runtime dependencies, and the protocol
surface we need (`initialize`, `tools/list`, `tools/call`, `ping`) is small. Nothing may be written to
stdout except JSON-RPC responses — a stray `print` corrupts the transport, so diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from . import config as config_mod
from . import evidence as evidence_mod
from . import export as export_mod
from . import graph as graph_mod
from . import index as index_mod
from .store import Entry, check_summary, parse_edge_spec, write_entry

PROTOCOL_VERSION = "2025-06-18"


def _log(msg: str) -> None:
    print(f"sigil-mcp: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- tool implementations

def _search(cfg, args: dict) -> dict:
    try:
        rows = index_mod.search(
            cfg.db_path, str(args.get("query") or ""), kind=args.get("kind") or None,
            state=args.get("state") or None, agent=args.get("agent") or None,
            project=args.get("project") or None, limit=int(args.get("limit") or 10),
            since=args.get("since") or None, until=args.get("until") or None)
    except ValueError as exc:  # a bad window is a caller error, never a silently ignored filter
        return {"query": args.get("query"), "hits": [], "error": f"since/until: {exc}"}
    if not rows:                      # the CLI exits 1 on "no matches"; here that is a normal result
        return {"query": args.get("query"), "hits": [], "note": "no matches"}
    keys: tuple[str, ...] = ("id", "kind", "state", "agent", "project", "created", "summary", "ask",
                             "snip", "score")
    return {"query": args.get("query"), "hits": [{k: r.get(k) for k in keys if k in r} for r in rows]}


def _timeline(cfg, args: dict) -> dict:
    rows = graph_mod.timeline(cfg, str(args.get("entity") or ""), at=args.get("at") or None)
    return {"entity": args.get("entity"), "edges": rows}


def _neighbors(cfg, args: dict) -> dict:
    return graph_mod.neighbors(cfg, str(args.get("entity") or ""), depth=int(args.get("depth") or 1))


def _write(cfg, args: dict) -> dict:
    """The same path as `sigil write`: the entry hits the markdown first, then its claims are asserted.

    A bad edge spec must not cost the memory, so the entry is written before any edge is validated and the
    caller learns which spec failed while the entry keeps its place on disk.
    """
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ValueError("write needs a summary — the retrieval handle of the entry")
    # same rule as `sigil write`, and here there is no opt-out: the size limit is what keeps a summary a
    # retrieval handle instead of a blob (#51)
    check_summary(summary)
    specs = list(args.get("edges") or [])
    parsed, order, broken = [], [], []
    for spec in specs:
        try:
            src, predicate, dst = parse_edge_spec(spec)
        except ValueError:
            broken.append(str(spec))
            order.append(str(spec))
            continue
        parsed.append((src, predicate, dst))
        order.append(str(spec))
    e = Entry(kind=str(args.get("kind") or "fact"), state=str(args.get("state") or "raw"),
              agent=str(args.get("agent") or ""), project=str(args.get("project") or ""),
              session=str(args.get("session") or ""), summary=summary,
              evidence=str(args.get("evidence") or ""), ask=str(args.get("ask") or ""),
              importance=float(args.get("importance") or 0.0), body=str(args.get("body") or ""),
              source="mcp", supersedes=[str(s) for s in (args.get("supersedes") or [])], edges=order,
              # the CLI write path and the reflection pass both record WHAT the evidence is (#109);
              # an MCP caller is a write path like any other, and 565 of 2064 live notes were missing
              # this field purely because they came through here (#128)
              evidence_kind=evidence_mod.classify(str(args.get("evidence") or "")))
    path = write_entry(cfg.root, e)
    failed = []
    for src, predicate, dst in parsed:
        try:
            graph_mod.add_edge(cfg, src, predicate, dst, note_id=e.id, at=e.created)
        except ValueError as exc:
            failed.append(f"{src}:{predicate}:{dst}: {exc}")
    # the graph follows supersedes (#20), the same way the CLI does: the old windows close and the
    # closures are written into the markdown, so a rebuild reproduces history instead of resurrecting it
    closed = graph_mod.close_superseded(cfg, e.supersedes, e) if e.supersedes else []
    # Always index. A CLI caller can run `sigil reindex` after a failed write; an MCP caller cannot,
    # so leaving the entry unindexed would mean a memory that was accepted and then forgotten. A bad
    # edge spec costs the claim, never the memory — the same trade the loader makes (#24, #30).
    index_mod.reindex(cfg.root, cfg.db_path)
    graph_mod.materialize(cfg)
    return {"id": e.id, "path": str(path), "state": e.state, "indexed": True,
            "skipped_edges": broken + failed, "closed_edges": closed}


def _stats(cfg, args: dict) -> dict:
    return export_mod.dataset_report(cfg)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": ("Recall from the long-term memory vault (BM25 over entries). Run this before "
                        "answering anything with project history. A topic phrase works best; if it "
                        "returns nothing, retry with fewer, more distinctive words."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "topic terms, not a sentence"},
                "limit": {"type": "integer", "default": 10},
                "kind": {"type": "string", "description": "decision|fact|conclusion|capture|handoff|..."},
                "state": {"type": "string", "description": "raw|promoted|pruned"},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "since": {"type": "string",
                          "description": "only entries created on/after this: 2026-10-01 or an ISO instant"},
                "until": {"type": "string",
                          "description": "only entries created through this DAY, or before an exact instant"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "timeline",
        "description": ("Every assertion about an entity with its validity window. Use it to see what "
                        "changed and when — closed windows are kept, never deleted."),
        "inputSchema": {
            "type": "object",
            "properties": {"entity": {"type": "string"},
                           "at": {"type": "string", "description": "ISO instant: the graph as of then"}},
            "required": ["entity"],
        },
    },
    {
        "name": "neighbors",
        "description": "The entities adjacent to one entity in the graph, to a given depth.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity": {"type": "string"}, "depth": {"type": "integer", "default": 1}},
            "required": ["entity"],
        },
    },
    {
        "name": "write",
        "description": ("Write one entry about one real outcome: a decision with its reason, a "
                        "measurement, a root cause. The summary is the retrieval handle, so write it as a "
                        "statement. Evidence is what makes it promotable; ask is the standalone question "
                        "it answers (without one it can never become a training example)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "one sentence stating the fact"},
                "kind": {"type": "string", "default": "fact"},
                "state": {"type": "string", "default": "raw",
                          "description": "promoted requires evidence — the caller judges, the vault records"},
                "evidence": {"type": "string",
                             "description": "how a skeptic re-checks it: numbers, paths, commands"},
                "ask": {"type": "string", "description": "a standalone question this entry answers"},
                "project": {"type": "string"}, "agent": {"type": "string"}, "session": {"type": "string"},
                "importance": {"type": "number"},
                "body": {"type": "string"},
                "supersedes": {"type": "array", "items": {"type": "string"}},
                "edges": {"type": "array", "items": {"type": "string"},
                          "description": "assertions to record, each \"SRC:predicate:DST\""},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "stats",
        "description": "Corpus shape: entries by state, promotion rate, exportable counts, graph size.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

DISPATCH: dict[str, Callable] = {
    "search": _search, "timeline": _timeline, "neighbors": _neighbors,
    "write": _write, "stats": _stats,
}


# --------------------------------------------------------------------------- protocol

def _result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(cfg, msg: dict) -> dict | None:
    """One request in, one response out (or None for a notification)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sigil", "version": __version__},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        fn = DISPATCH.get(name)
        if fn is None:
            return _error(msg_id, -32602, f"unknown tool {name!r}")
        try:
            payload = fn(cfg, params.get("arguments") or {})
        except Exception as exc:                       # a bad request must not kill the server
            return _result(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                    "isError": True})
        return _result(msg_id, {"content": [{"type": "text",
                                             "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                                "isError": False})
    return _error(msg_id, -32601, f"method not found: {method}")


def serve(vault: str | Path) -> int:
    try:
        cfg = config_mod.load(vault, create=False)
    except config_mod.VaultNotFound as exc:
        # A server that starts against a path it did not find answers every query from nothing and
        # looks healthy doing it (#141). Refuse, name the path, and let the harness show the failure.
        _log(f"refusing to serve — {exc}")
        return 2
    note = index_mod.refresh_if_stale(cfg)
    if note:
        _log(note)
    _log(f"serving {cfg.root} (sigil {__version__}, protocol {PROTOCOL_VERSION})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = _error(None, -32700, f"parse error: {exc}")
        else:
            if isinstance(msg, list):
                resp = _error(None, -32600, "batch requests are not supported")
            else:
                resp = handle(cfg, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
