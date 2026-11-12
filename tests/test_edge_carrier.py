"""An edge whose carrier names no entry is refused, and one already in the index is re-homed (#94).

The defect: `graph.add_edge()` handed `note_id` straight to `record_edge()`, `find_entry()` found
nothing, and that returned False with nobody told. `quipu edge` then printed the new id and exited 0,
so the edge existed only in SQLite — `rm index.db` + `reindex` deleted a live belief while `audit`
reported it and no write path did.

Two properties, both from the invariants:

1. a carrier that is GIVEN but does not resolve is a usage error that writes nothing — the db must be
   unchanged after the refusal, not merely raise, and the old belief must stay live when the failing
   call is a contradiction (closing first and validating after left an entity with no live edge at all);
2. a vault already holding such an edge can be repaired from the edge's own `src`, which is the entry
   that asserted it; the repair survives a rebuild, because the markdown is the truth.

A carrier that is absent is a different case and stays allowed: `quipu edge a part_of b` with no
`--note` warns on stderr, and the docs show that form.

QA scope: tests/ only — the builder owns quipu/**.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sigil import config as config_mod
from sigil import graph as graph_mod

ASK = "why did the vault lose a live edge on reindex?"
SUMMARY = "a live edge exists only in the index when its carrier names no entry"


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "sigil", *args], capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    assert run_cli("--vault", str(v), "init").returncode == 0
    return v


def seed_entry(vault) -> str:
    proc = run_cli("--vault", str(vault), "write", "--kind", "decision", "--state", "promoted",
                   "--summary", SUMMARY, "--evidence", "audit: edges_not_in_markdown 4 of 8",
                   "--ask", ASK, "--importance", "0.9")
    assert proc.returncode == 0, proc.stderr
    return Path(proc.stdout.strip()).name.split("-")[0]


def edge_rows(vault):
    """(id, src, predicate, dst, note_id, valid_to) for every edge, live or closed."""
    c = sqlite3.connect(config_mod.load(vault).db_path)
    c.row_factory = sqlite3.Row
    try:
        return [tuple(r) for r in c.execute(
            "SELECT id, src, predicate, dst, note_id, valid_to FROM edges ORDER BY id")]
    finally:
        c.close()


def note_path(vault, eid) -> Path:
    return next((Path(vault) / "notes").glob(f"{eid}*.md"))


def test_an_unknown_carrier_is_refused_and_writes_nothing(vault):
    seed_entry(vault)
    proc = run_cli("--vault", str(vault), "edge", "quipu", "depends_on", "sqlite",
                   "--note", "a summary sentence, not an entry id")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "a summary sentence, not an entry id" in proc.stderr
    assert edge_rows(vault) == [], "a refused assertion must not land a row"


def test_a_contradiction_with_an_unknown_carrier_leaves_the_old_belief_live(vault):
    eid = seed_entry(vault)
    ok = run_cli("--vault", str(vault), "edge", "trx40", "measured_as", "docp-3600", "--note", eid)
    assert ok.returncode == 0, ok.stderr
    old = edge_rows(vault)[0][0]

    proc = run_cli("--vault", str(vault), "contradict", str(old), "trx40", "measured_as", "docp-3200",
                   "--note", "not an entry")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    rows = edge_rows(vault)
    assert len(rows) == 1, "a refused replacement must not open a second edge"
    assert rows[0][5] is None, "the old belief must stay live"
    assert "trx40:measured_as:docp-3600" in note_path(vault, eid).read_text()


def test_repair_edges_reports_a_db_only_edge_and_writes_nothing(vault):
    eid = seed_entry(vault)
    cfg = config_mod.load(vault)
    # the shape #94 leaves behind: a row with a carrier, recorded nowhere in that carrier's markdown
    graph_mod.add_edge(cfg, eid, "depends_on", "someone", note_id=eid, record=False)
    before = note_path(vault, eid).read_text()

    proc = run_cli("--vault", str(vault), "repair", "--edges")
    assert proc.returncode == 0, proc.stderr
    assert eid in proc.stdout and "depends_on" in proc.stdout
    assert note_path(vault, eid).read_text() == before, "a dry run must not write"


def test_repair_edges_apply_survives_a_rebuild(vault):
    eid = seed_entry(vault)
    cfg = config_mod.load(vault)
    graph_mod.add_edge(cfg, eid, "depends_on", "someone", note_id=eid, record=False)

    proc = run_cli("--vault", str(vault), "repair", "--edges", "--apply")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["rehomed"] == 1 and report["unresolved"] == 0
    assert f"{eid}:depends_on:someone" in note_path(vault, eid).read_text()

    Path(cfg.db_path).unlink()
    assert run_cli("--vault", str(vault), "reindex").returncode == 0
    assert [r[3] for r in edge_rows(vault)] == ["someone"], "the markdown is the truth"


def test_the_audit_agrees_once_the_repair_has_run(vault):
    """The defect #94's first pass left: the repair wrote the markdown, and `audit` kept reporting
    the edge, because it resolves the carrier from `note_id` alone while the repair uses `src`."""
    eid = seed_entry(vault)
    cfg = config_mod.load(vault)
    graph_mod.add_edge(cfg, eid, "depends_on", "someone", note_id="prose, not an id", record=False)
    assert json.loads(run_cli("--vault", str(vault), "audit").stdout)["edges_not_in_markdown"]

    proc = run_cli("--vault", str(vault), "repair", "--edges", "--apply")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rehomed"] == 1

    report = json.loads(run_cli("--vault", str(vault), "audit").stdout)
    assert report["edges_not_in_markdown"] == [], "the audit must agree with the repair it just ran"


def test_an_empty_subject_is_refused(vault):
    """`quipu edge "" contradicts x` created a live edge with no subject; only a rebuild dropped it."""
    seed_entry(vault)
    proc = run_cli("--vault", str(vault), "edge", "", "contradicts", "35a44ae6", "--note", "")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "non-empty" in proc.stderr
    assert edge_rows(vault) == []


# --- #115: one claim, three correct spellings -----------------------------------------------------
# The markdown keeps the model's prose, add_edge stores a slug and a rebuild stores the identity key.
# Comparing spellings made audit report nine live edges as unrecorded, and a rebuild minted a second
# row for each. These are the four pairs measured on the live vault.

@pytest.mark.parametrize("db_form, markdown_form", [
    ("quipu:blocks:memoryprovider", "quipu:blocks:memory-provider"),
    ("flash-next:supersedes:launch-flashnext", "flashnext:supersedes:launch-flashnext"),
    ("gpu1:located_in:trx40-pro-s", "GPU 1:located_in:TRX40-PRO S"),
    ("coder-xxs:implements:qwen3-8-flash-next-iq3-xxs",
     "coder-xxs:implements:qwen3.8-flash-next-iq3_xx_s"),
    ("n-gram:measured_as:context-echo", "ngram:measured_as:context-echo"),
])
def test_a_claim_is_recognised_across_spellings(vault, db_form, markdown_form):
    from sigil.store import Entry, find_entry, read_entry, write_entry
    root = vault / "vault"
    cfg = config_mod.load(root, create=True)
    entry = Entry(id="beef1234", state="promoted", summary="the claim", evidence="fixture",
                  edges=[markdown_form])
    write_entry(root, entry)
    loaded = read_entry(find_entry(root, "beef1234"))
    assert graph_mod.entry_records_edge(loaded, db_form), (
        f"the db spelling {db_form!r} is the same claim as the markdown's {markdown_form!r}")

    # and the same claim asserted from the db spelling lands as one row, not two
    src, predicate, dst = db_form.split(":")
    graph_mod.add_edge(cfg, src, predicate, dst, note_id="beef1234")
    graph_mod.rebuild(cfg)
    with sqlite3.connect(cfg.db_path) as c:
        rows = c.execute("SELECT src, predicate, dst FROM edges").fetchall()
    assert len(rows) == 1, f"one claim must be one row, got {rows}"
