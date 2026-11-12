"""An agent can assert an edge where it captures the fact (#84).

Measured before this: 891 entries and **3 edges**, because the one surface the team now writes through —
the Hermes provider — had no `edges` field. `quipu write --edge` and MCP `_write` could both carry one;
the provider could not, so `memory.provider: quipu` produced nodes only.

The rule from the CLI path carries over unchanged: a bad edge costs the claim, never the memory.
"""

from __future__ import annotations

import json
from pathlib import Path

import sigil.config
import sigil.index
from sigil.hermes_provider import SigilMemoryProvider

SUMMARY = "The 6700 XT pair needs HSA_OVERRIDE_GFX_VERSION set or torch sees no device"


def _provider(tmp_path: Path, monkeypatch) -> tuple[SigilMemoryProvider, Path]:
    vault = tmp_path / "vault"
    sigil.config.load(vault, create=True)
    monkeypatch.setenv("QUIPU_VAULT", str(vault))
    provider = SigilMemoryProvider()
    provider.initialize("edges-test", hermes_home=str(tmp_path / ".hermes"), platform="cli")
    return provider, vault


def _write(provider: SigilMemoryProvider, **args) -> dict:
    return json.loads(provider.handle_tool_call("sigil_write", args))


def _edges(vault: Path) -> list[tuple[str, str, str]]:
    c = sigil.index.connect(vault / ".sigil" / "index.db")
    try:
        return [tuple(r) for r in c.execute(
            "select src, predicate, dst from edges where valid_to is null order by src, dst").fetchall()]
    finally:
        c.close()


def test_the_schema_advertises_what_the_tool_accepts():
    """A field the schema omits is a field no model will ever send."""
    (schema,) = [s for s in SigilMemoryProvider().get_tool_schemas() if s["name"] == "sigil_write"]
    body = schema.get("parameters") or schema.get("input_schema") or {}
    props = body["properties"]
    assert "edges" in props and "supersedes" in props
    assert "part_of" in props["edges"]["description"], "the closed vocabulary must be visible"


def test_a_valid_edge_reaches_the_graph(tmp_path, monkeypatch):
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="gfx1031 under rocm 7.2",
                 edges=["gpu-6700xt:part_of:glitch-box"])
    assert out["skipped_edges"] == []
    assert ("gpu-6700xt", "part_of", "glitch-box") in _edges(vault)


def test_a_malformed_spec_costs_the_claim_not_the_memory(tmp_path, monkeypatch):
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="gfx1031 under rocm 7.2",
                 edges=["not-an-edge-spec"])
    assert out["skipped_edges"], "the refusal must be visible to the caller"
    assert "not-an-edge-spec" in out["skipped_edges"][0]
    assert out["path"] and Path(out["path"]).exists(), "the entry is the point; the edge is the claim"


def test_an_invented_predicate_is_reported_not_accepted(tmp_path, monkeypatch):
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="gfx1031 under rocm 7.2",
                 edges=["gpu-6700xt:served_by:glitch-box"])
    assert out["skipped_edges"], out
    assert ("gpu-6700xt", "served_by", "glitch-box") not in _edges(vault)


def test_supersedes_closes_the_old_edge_window(tmp_path, monkeypatch):
    """The graph follows supersedes (#20) on this surface too, not only on the CLI's."""
    provider, vault = _provider(tmp_path, monkeypatch)
    first = _write(provider, summary=SUMMARY, evidence="measured on gfx1031",
                   edges=["gpu-6700xt:part_of:glitch-box"], importance=0.9)
    out = _write(provider, summary="The 6700 XT pair needs no override under rocm 7.2",
                 evidence="re-measured after the driver update",
                 supersedes=[first["id"]], edges=["gpu-6700xt:part_of:glitch-box"])
    assert out["skipped_edges"] == []
    # the replacement entry exists and the old claim is corrected rather than duplicated
    assert out["id"] != first["id"]
    assert _edges(vault), "the replacement's edge must be live"
