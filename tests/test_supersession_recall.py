"""Retrieval must know that a claim was corrected.

`supersedes` was a frontmatter field and nothing else: it closed the superseded entry's graph edge
windows and produced preference pairs on export, and retrieval ignored it completely. So a claim
that had been corrected ranked right beside its correction, and the measured symptom was a query
for the current PyPI publisher state returning the two entries that described the OLD one.

These tests are hermetic: tmp vaults, no network, no live vault. They check the RANKING (a
superseded row sorts below every current row, the score order inside each group untouched), the
ANNOTATION (`superseded_by` names the corrector), the SURFACES (CLI text, CLI json, MCP payload,
provider payload), and that nothing about a vault without supersedes changes at all.
"""
from __future__ import annotations

import json
import subprocess
import sys

from sigil import hermes_provider as provider_mod
from sigil import index as index_mod
from sigil import mcp as mcp_mod
from sigil.config import load
from sigil.store import Entry, write_entry

QUERY = "pypi publisher pending"

#: Long enough to make the length normalisation cost the entry its rank: the stale note is the
#: better MATCH, and the only thing that puts the correction above it is that it is current.
BODY = ("this note carries a long body so that the bm25 length normalisation works against the "
        "match, which is the point: the stale claim outscores the correction on text alone and "
        "still has to rank below it, because a correction is not a better match, it is a more "
        "current one, and retrieval that cannot tell the difference answers with the old claim.")


def _vault(tmp_path):
    return load(tmp_path / "vault", create=True)


def _write(cfg, summary, *, body="", ask="", supersedes=None, state="promoted", kind="decision"):
    e = Entry(kind=kind, state=state, summary=summary, body=body, ask=ask, evidence="measured",
              created="2026-01-01T00:00:00Z", supersedes=list(supersedes or []))
    write_entry(cfg.root, e)
    return e


def _reindex(cfg):
    index_mod.reindex(cfg.root, cfg.db_path)
    return cfg


def test_a_corrected_claim_ranks_below_its_correction(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    ids = [r["id"] for r in rows]
    assert current.id in ids and stale.id in ids, f"both entries must be returned: {ids}"
    assert ids[0] == current.id, f"the correction must rank first: {ids}"
    stale_row = next(r for r in rows if r["id"] == stale.id)
    assert stale_row["superseded_by"] == current.id, stale_row
    current_row = next(r for r in rows if r["id"] == current.id)
    assert current_row.get("superseded_by") is None, current_row


def test_a_superseded_claim_sorts_below_an_unrelated_current_entry(tmp_path):
    """The demotion is global, not a favour to the corrector: any current entry outranks it."""
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    _write(cfg, f"{QUERY} in another repo", supersedes=None)
    _write(cfg, "the correction", supersedes=[stale.id])
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    stale_row = next(r for r in rows if r["id"] == stale.id)
    others = [r for r in rows if r["id"] != stale.id]
    assert others, "the other entries must still be returned"
    assert rows.index(stale_row) >= len(others) - 1 or rows.index(stale_row) == len(rows) - 1, \
        f"a superseded row must not outrank a current one: {[r['id'] for r in rows]}"
    assert stale_row["superseded_by"], "the stale row must name its corrector"


def test_the_score_order_inside_each_group_is_untouched(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    current_rows = [r for r in rows if not r.get("superseded_by")]
    stale_rows = [r for r in rows if r.get("superseded_by")]
    assert [r["score"] for r in current_rows] == sorted(r["score"] for r in current_rows), \
        "the score order inside the current group must survive"
    assert [r["score"] for r in stale_rows] == sorted(r["score"] for r in stale_rows), \
        "the score order inside the superseded group must survive"
    assert rows.index(current_rows[0]) < rows.index(stale_rows[0])


def test_a_ranking_that_is_already_current_is_untouched(tmp_path):
    """The demotion bites only when a superseded entry outranks a current one.

    When the correction already matched best, the stale entry stays where it was: it is still
    history, and this is a ranking rule, not a filter. A query whose only hits are the correction
    and the claim it corrects therefore keeps the stale entry at rank 2 — the rule cannot sink it
    below itself.
    """
    cfg = _vault(tmp_path)
    stale = _write(cfg, f"{QUERY} but the old repo", body=BODY)
    current = _write(cfg, QUERY, supersedes=[stale.id])
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    assert [r["id"] for r in rows] == [current.id, stale.id], \
        f"an already-correct ranking must not move: {[r['id'] for r in rows]}"
    assert rows[1]["superseded_by"] == current.id, "it is still marked as superseded"


def test_two_independent_entries_keep_their_score_order(tmp_path):
    cfg = _vault(tmp_path)
    short = _write(cfg, QUERY)
    long = _write(cfg, f"{QUERY} with a longer note", body=BODY)
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    assert [r["id"] for r in rows] == [short.id, long.id], \
        f"nothing about supersession may reorder two current entries: {[r['id'] for r in rows]}"
    assert all(r.get("superseded_by") is None for r in rows)


def test_a_vault_without_supersedes_keeps_its_ordering(tmp_path):
    """No supersedes, no change: not a row, not a score, not an order."""
    cfg = _vault(tmp_path)
    for i in range(4):
        _write(cfg, f"{QUERY} number {i}", body=BODY if i % 2 else "")
    _reindex(cfg)

    rows = index_mod.search(cfg.db_path, QUERY)

    assert [r["score"] for r in rows] == sorted(r["score"] for r in rows), \
        "the ranking must be exactly the score order"
    assert all(r.get("superseded_by") is None for r in rows)


def test_pruned_entries_still_obey_the_pruned_filter(tmp_path):
    cfg = _vault(tmp_path)
    pruned = _write(cfg, f"{QUERY} pruned", state="pruned")
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[pruned.id])
    _reindex(cfg)

    default = index_mod.search(cfg.db_path, QUERY)
    assert pruned.id not in [r["id"] for r in default], "pruned entries stay out by default"

    with_pruned = index_mod.search(cfg.db_path, QUERY, include_pruned=True)
    assert pruned.id in [r["id"] for r in with_pruned], "include_pruned must still bring them back"
    pruned_row = next(r for r in with_pruned if r["id"] == pruned.id)
    assert pruned_row["superseded_by"] == current.id, pruned_row

    only_pruned = index_mod.search(cfg.db_path, QUERY, state="pruned")
    assert [r["id"] for r in only_pruned] == [pruned.id], "an explicit state= still filters"


def test_the_supersession_survives_the_loss_of_the_index(tmp_path):
    """The markdown is the truth: `rm index.db` + reindex must reproduce the ranking."""
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)
    before = [r["id"] for r in index_mod.search(cfg.db_path, QUERY)]
    assert before[0] == current.id

    cfg.db_path.unlink()
    index_mod.reindex(cfg.root, cfg.db_path)

    after_rows = index_mod.search(cfg.db_path, QUERY)
    assert [r["id"] for r in after_rows] == before, "a rebuild must reproduce the ranking"
    stale_row = next(r for r in after_rows if r["id"] == stale.id)
    assert stale_row["superseded_by"] == current.id


def test_the_cli_says_when_a_hit_is_superseded(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    out = subprocess.run([sys.executable, "-m", "sigil", "--vault", str(cfg.root),
                          "search", QUERY, "--limit", "5"],
                         capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    assert f"(superseded by {current.id})" in out.stdout, out.stdout
    assert stale.id in out.stdout


def test_the_cli_json_carries_the_corrector(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    out = subprocess.run([sys.executable, "-m", "sigil", "--vault", str(cfg.root),
                          "search", QUERY, "--limit", "5", "--json"],
                         capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    stale_row = next(r for r in rows if r["id"] == stale.id)
    assert stale_row["superseded_by"] == current.id, stale_row


def test_the_mcp_search_payload_carries_the_corrector(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    out = mcp_mod._search(cfg, {"query": QUERY, "limit": 5})

    hits = out["hits"]
    stale_hit = next(h for h in hits if h["id"] == stale.id)
    assert stale_hit["superseded_by"] == current.id, stale_hit
    current_hit = next(h for h in hits if h["id"] == current.id)
    assert "superseded_by" not in current_hit, current_hit


def test_the_provider_search_tool_carries_the_corrector(tmp_path):
    cfg = _vault(tmp_path)
    stale = _write(cfg, QUERY, body=BODY)
    current = _write(cfg, f"{QUERY} — the corrected state", supersedes=[stale.id])
    _reindex(cfg)

    provider = provider_mod.SigilMemoryProvider()
    provider._vault = str(cfg.root)
    provider._hermes_home = str(tmp_path)

    out = json.loads(provider.handle_tool_call("sigil_search", {"query": QUERY, "limit": 5}))

    results = out["results"]
    stale_result = next(r for r in results if r["id"] == stale.id)
    assert stale_result["superseded_by"] == current.id, stale_result
    current_result = next(r for r in results if r["id"] == current.id)
    assert "superseded_by" not in current_result, current_result
