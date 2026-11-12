"""Dataset export.

Three products from one corpus:
  sft.jsonl    chat-format supervised examples, from entries whose ask is known
  dpo.jsonl    preference pairs, from supersedes/invalidations (stale claim vs corrected claim)
  graph/       nodes.jsonl + edges.jsonl, temporal windows preserved

Every record carries provenance so a dataset can be rebuilt byte-for-byte from the vault.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .index import connect
from .store import Entry, iter_entries


def _meta(e: Entry, **extra) -> dict:
    return {
        "sigil": {
            "entry": e.id, "kind": e.kind, "state": e.state, "agent": e.agent,
            "project": e.project, "session": e.session, "created": e.created,
            "confidence": e.confidence, "importance": e.importance, "source": e.source,
            "evidence_kind": e.evidence_kind, "evidence_bytes": len(e.evidence.encode("utf-8")),
        },
        **extra,
    }


MIN_ASK_CHARS = 20


def _ask_ok(e: Entry) -> bool:
    """A training prompt has to stand alone. "did that fix it" is a turn, not a question:
    pairing it with a distilled answer teaches the model to answer out of nowhere."""
    ask = (e.ask or "").strip()
    return len(ask) >= MIN_ASK_CHARS and len(ask.split()) >= 3


def _answer(e: Entry) -> str:
    parts = [e.summary]
    if e.evidence:
        parts.append(f"Evidence: {e.evidence}")
    body = e.stripped_body()
    if body:
        parts.append(body)
    return "\n\n".join(p for p in parts if p)


def export_sft(cfg: Config, out: Path, system: str | None = None,
               evidence_kinds: set[str] | None = None, min_evidence_bytes: int = 0) -> int:
    """promoted entries that record the request they answer become (ask -> answer) pairs.

    `evidence_kinds` and `min_evidence_bytes` filter the training set without touching the vault: an
    entry with `evidence_kind: claim` is a memory worth keeping and a weaker training target (#109).
    The field is recorded either way, so the same corpus can be exported at two strengths.
    """
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for _path, e in iter_entries(cfg.root):
            if e.state != "promoted" or not e.summary or not _ask_ok(e):
                continue
            if evidence_kinds and e.evidence_kind not in evidence_kinds:
                continue
            if len(e.evidence.encode("utf-8")) < min_evidence_bytes:
                continue
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages += [{"role": "user", "content": e.ask},
                         {"role": "assistant", "content": _answer(e)}]
            fh.write(json.dumps({"messages": messages, "meta": _meta(e)}, ensure_ascii=False) + "\n")
            n += 1
    return n


def export_preferences(cfg: Config, out: Path) -> int:
    """supersedes -> (prompt, chosen, rejected). The stale claim is the rejected side.

    This is the export nothing else in the space produces: a memory that changed its mind has
    already labelled which answer was better and why.
    """
    by_id = {e.id: e for _p, e in iter_entries(cfg.root)}
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for _path, e in by_id.items():
            for old_id in e.supersedes:
                old = by_id.get(old_id)
                if old is None:
                    continue
                fh.write(json.dumps({
                    "prompt": e.ask or old.ask or f"What is the current state of {e.project or 'this work'}?",
                    "chosen": _answer(e),
                    "rejected": _answer(old),
                    "meta": _meta(e, superseded=_meta(old)["sigil"], reason=e.evidence),
                }, ensure_ascii=False) + "\n")
                n += 1
    return n


def export_graph(cfg: Config, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    c = connect(cfg.db_path)
    ents = [dict(r) for r in c.execute("SELECT * FROM entities ORDER BY name").fetchall()]
    edges = [dict(r) for r in c.execute("SELECT * FROM edges ORDER BY valid_from").fetchall()]
    c.close()
    with (out_dir / "nodes.jsonl").open("w", encoding="utf-8") as fh:
        for e in ents:
            e["aliases"] = json.loads(e["aliases"] or "[]")
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    with (out_dir / "edges.jsonl").open("w", encoding="utf-8") as fh:
        for e in edges:
            e["live"] = e["valid_to"] is None
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return {"nodes": len(ents), "edges": len(edges)}


def dataset_report(cfg: Config) -> dict:
    entries = [e for _p, e in iter_entries(cfg.root)]
    promoted = [e for e in entries if e.state == "promoted"]
    sft = [e for e in promoted if e.summary and _ask_ok(e)]
    with_evidence = [e for e in promoted if e.evidence]
    pairs = sum(len(e.supersedes) for e in promoted)
    c = connect(cfg.db_path)
    live = c.execute("SELECT count(*) FROM edges WHERE valid_to IS NULL").fetchone()[0]
    closed = c.execute("SELECT count(*) FROM edges WHERE valid_to IS NOT NULL").fetchone()[0]
    c.close()
    from . import graph as graph_mod  # local import: graph imports index, so a top-level one cycles
    graph_report = graph_mod.connectivity(cfg)
    # #109: what the evidence IS, so "present" and "re-checkable" stop being the same word
    kinds = {}
    for e in promoted:
        key = e.evidence_kind or "unclassified"
        kinds[key] = kinds.get(key, 0) + 1
    sizes = sorted(len(e.evidence.encode("utf-8")) for e in with_evidence)
    evidence_bytes = {
        "min": sizes[0] if sizes else 0,
        "median": sizes[len(sizes) // 2] if sizes else 0,
        "p90": sizes[int(len(sizes) * 0.9)] if sizes else 0,
        "max": sizes[-1] if sizes else 0,
    }
    return {
        "entries": len(entries),
        "promoted": len(promoted),
        "raw": sum(1 for e in entries if e.state == "raw"),
        "pruned": sum(1 for e in entries if e.state == "pruned"),
        "sft_examples": len(sft),
        "promoted_with_evidence": len(with_evidence),
        "evidence_by_kind": kinds,
        "evidence_bytes": evidence_bytes,
        "preference_pairs": pairs,
        "edges_live": live,
        "edges_closed": closed,
        "edges_by_predicate": graph_report["edges_by_predicate"],
        "graph_nodes": graph_report["nodes"],
        "entries_with_an_edge": graph_report["entries_with_an_edge"],
        "projects": graph_report["projects"],
        "promotion_rate": round(len(promoted) / len(entries), 3) if entries else 0.0,
    }
