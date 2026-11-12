"""`--supersedes` closes the belief it corrects, and a rebuild replays the closure (#20).

The defect this pins: `write --supersedes <old>` recorded the preference pair and left the old
entry's graph edge LIVE, so `timeline` and `export graph` showed two live windows for one fact and
the stale claim was indistinguishable from the correction.

Two properties are asserted, not one:

1. the old window closes at the instant the correction was written, and the correction's own edge
   is the live one;
2. the closure lives in the MARKDOWN. `rm .sigil/index.db` + `reindex` must reproduce the same
   graph, or the fix traded a visible bug for a silent data loss on the next rebuild.

An edge the correction RESTATES is left live on purpose: `_assert_edge` is idempotent by triple, so
closing it would leave the fact closed with nothing live.

QA scope: tests/ only. Behaviour is driven through the CLI and the MCP tool handler — the two
surfaces an agent actually uses.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sigil import config as config_mod
from sigil import graph as graph_mod
from sigil import index as index_mod
from sigil import mcp as mcp_mod
from sigil.store import Entry, write_entry

OLD_SUMMARY = "DOCP 3200 with FCLK 1600 is stable on the TRX40"
NEW_SUMMARY = "DOCP 3600 is stable again after the BIOS 2601 flash"
ASK = "why did the box reboot at 03:02?"


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "sigil", *args], capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    assert run_cli("--vault", str(v), "init").returncode == 0
    return v


def write(vault, *args):
    proc = run_cli("--vault", str(vault), "write", *args)
    assert proc.returncode == 0, proc.stderr
    return proc


def old_id_of(proc):
    """The entry id from the markdown path the CLI printed (notes/<id>-<slug>.md)."""
    return Path(proc.stdout.strip()).name.split("-")[0]


def seed(vault):
    proc = write(vault, "--kind", "decision", "--state", "promoted", "--summary", OLD_SUMMARY,
                 "--evidence", "0 MC25 lines in 3 boots", "--ask", ASK, "--importance", "0.9",
                 "--edge", "TRX40:measured_as:DOCP 3200")
    return old_id_of(proc)


def graph_rows(db):
    """The graph as comparable data: (src, predicate, dst, valid_from, valid_to), ids excluded."""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT src, predicate, dst, valid_from, valid_to FROM edges "
                         "ORDER BY src, predicate, dst, valid_from").fetchall()
    finally:
        c.close()
    return [tuple(r) for r in rows]


def test_supersede_closes_the_old_edge_at_the_correction(vault):
    old = seed(vault)
    proc = write(vault, "--kind", "decision", "--state", "promoted", "--summary", NEW_SUMMARY,
                 "--evidence", "3 consecutive boots, 0 MCE lines each", "--ask", ASK,
                 "--importance", "0.95", "--supersedes", old,
                 "--edge", "TRX40:measured_as:DOCP 3600")
    # the closure is reported on stderr, and stdout still carries only the path an agent parses
    assert "closed edge" in proc.stderr, proc.stderr
    assert proc.stdout.strip().endswith(".md")

    out = run_cli("--vault", str(vault), "timeline", "TRX40")
    assert out.returncode == 0, out.stderr
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    stale = [ln for ln in lines if "docp-3200" in ln]
    fresh = [ln for ln in lines if "docp-3600" in ln]
    assert len(stale) == 1 and len(fresh) == 1, lines
    assert "→ now" not in stale[0], f"the superseded belief must not read 'now': {stale[0]}"
    assert "→ now" in fresh[0], f"the correction is the live belief: {fresh[0]}"


def test_supersede_with_restated_edge_keeps_the_belief_live(vault):
    """A correction that restates the same triple is the same belief under a new carrier."""
    old = seed(vault)
    write(vault, "--kind", "decision", "--state", "promoted",
          "--summary", "Re-confirmed: DOCP 3200 still stable, 20 boots later",
          "--evidence", "20 consecutive boots, 0 MCE lines each", "--ask", ASK,
          "--importance", "0.95", "--supersedes", old,
          "--edge", "TRX40:measured_as:DOCP 3200")

    rows = graph_rows(vault / ".sigil" / "index.db")
    assert len(rows) == 1, rows
    assert rows[0][4] is None, f"one belief, still live: {rows}"


def test_the_closure_survives_rm_index_db_and_reindex(vault):
    old = seed(vault)
    write(vault, "--kind", "decision", "--state", "promoted", "--summary", NEW_SUMMARY,
          "--evidence", "3 consecutive boots, 0 MCE lines each", "--ask", ASK,
          "--importance", "0.95", "--supersedes", old,
          "--edge", "TRX40:measured_as:DOCP 3600")

    db = vault / ".sigil" / "index.db"
    before = graph_rows(db)
    assert any(r[4] for r in before), f"the write path closed a window: {before}"

    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        path.unlink(missing_ok=True)
    reindexed = run_cli("--vault", str(vault), "reindex")
    assert reindexed.returncode == 0, reindexed.stderr
    after = graph_rows(db)
    assert after == before, f"a rebuild must reproduce the correction\nbefore={before}\nafter={after}"


def test_audit_flags_a_superseded_entry_whose_edge_is_still_live(vault):
    """Entries written straight through `store` bypass the fix — the shape of a pre-fix vault.
    The invariant has to be visible in `audit`, or such a vault stays broken while looking healthy.
    """
    cfg = config_mod.load(vault, create=True)
    old = Entry(kind="decision", state="promoted", summary=OLD_SUMMARY,
                evidence="0 MC25 lines in 3 boots", ask=ASK, importance=0.9,
                edges=["trx40:measured_as:docp-3200"])
    write_entry(cfg.root, old)
    graph_mod.add_edge(cfg, "TRX40", "measured_as", "DOCP 3200", note_id=old.id, at=old.created)

    new = Entry(kind="decision", state="promoted", summary=NEW_SUMMARY,
                evidence="3 consecutive boots, 0 MCE lines each", ask=ASK, importance=0.95,
                supersedes=[old.id])
    write_entry(cfg.root, new)
    index_mod.reindex(cfg.root, cfg.db_path)

    proc = run_cli("--vault", str(vault), "audit")
    assert proc.returncode == 1, proc.stdout
    problems = json.loads(proc.stdout)["problems"]
    assert any(f"supersedes {old.id} but its edge is still live" in p for p in problems), problems


def test_audit_is_clean_after_a_supersede_written_by_the_cli(vault):
    """The fix and the check have to agree: no 'still live' problem after the real write path."""
    old = seed(vault)
    write(vault, "--kind", "decision", "--state", "promoted", "--summary", NEW_SUMMARY,
          "--evidence", "3 consecutive boots, 0 MCE lines each", "--ask", ASK,
          "--importance", "0.95", "--supersedes", old,
          "--edge", "TRX40:measured_as:DOCP 3600")

    proc = run_cli("--vault", str(vault), "audit")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["problems"] == []


def test_mcp_write_closes_the_superseded_edge_and_reports_it(vault):
    """The MCP caller cannot run `quipu reindex` afterwards, so the write path has to behave."""
    cfg = config_mod.load(vault, create=True)
    first = mcp_mod._write(cfg, {"summary": OLD_SUMMARY, "evidence": "0 MC25 lines in 3 boots",
                                 "state": "promoted", "ask": ASK, "importance": 0.9,
                                 "edges": ["TRX40:measured_as:DOCP 3200"]})
    assert first["skipped_edges"] == [], first

    second = mcp_mod._write(cfg, {"summary": NEW_SUMMARY,
                                  "evidence": "3 consecutive boots, 0 MCE lines each",
                                  "state": "promoted", "ask": ASK, "importance": 0.95,
                                  "supersedes": [first["id"]],
                                  "edges": ["TRX40:measured_as:DOCP 3600"]})
    assert second["skipped_edges"] == [], second
    assert len(second["closed_edges"]) == 1, second["closed_edges"]
    assert second["closed_edges"][0]["spec"] == "trx40:measured_as:docp-3200", second["closed_edges"]

    rows = graph_rows(cfg.db_path)
    assert len(rows) == 2, rows
    closed = [r for r in rows if r[2] == "docp-3200"]
    live = [r for r in rows if r[2] == "docp-3600"]
    assert closed and closed[0][4], f"the superseded window is closed: {rows}"
    assert live and live[0][4] is None, f"the correction is live: {rows}"
