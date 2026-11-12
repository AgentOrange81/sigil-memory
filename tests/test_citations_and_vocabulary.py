"""Citations are a view, and an off-vocabulary predicate is a finding (#85, option 2).

Option 2 of #85 was `mentions` edges for the 117 resolvable id-pairs on the live vault. A citation is
reproducible from the body that makes it — `[[e2314ae5]]` in prose stays true forever — so persisting
it would duplicate a derivable fact into markdown that `rebuild` then re-folds. Same reasoning as the
project hub, so the same shape: derived at read time, nothing written.

The audit rule is the other half: the vocabulary is closed per vault (`.sigil/config.toml` can extend
it, which is how the live vault legitimately carries `served_by`), and until now nothing reported an edge
whose predicate is in neither place — a graph rots into 400 spellings of "uses" quietly.
"""

from __future__ import annotations

import json
from pathlib import Path

import sigil.config
from sigil import graph as graph_mod
from sigil.cli import main

BASE = "DOCP 3600 threw 649 correctable errors in a single boot on the TRX40"
LATER = "DOCP 3200 with FCLK 1600 is stable; 3600 stays dropped until the BIOS fixes the divider"
UNRELATED = "The 6700 XT pair needs HSA_OVERRIDE_GFX_VERSION or torch sees no device"


def _seed(tmp_path: Path) -> tuple[Path, str, str, str]:
    vault = tmp_path / "vault"
    sigil.config.load(vault, create=True)
    from sigil.store import read_entry
    for summary in (BASE, LATER, UNRELATED):
        main(["--vault", str(vault), "write", "--summary", summary, "--state", "promoted",
              "--kind", "decision", "--project", "wraith", "--importance", "0.9",
              "--evidence", "measured on the TRX40 in three consecutive boots"])
    by_summary = {read_entry(p).summary: read_entry(p).id
                  for p in (vault / "notes").glob("*.md")}
    return vault, by_summary[BASE], by_summary[LATER], by_summary[UNRELATED]


def _files(vault: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in (vault / "notes").glob("*.md")}


def test_citations_find_both_directions(tmp_path):
    vault, base, later, _unrelated = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    # the correction names the entry it corrects, which is how entries point at each other
    main(["--vault", str(vault), "write", "--summary", LATER, "--state", "promoted",
          "--kind", "decision", "--project", "wraith", "--importance", "0.9",
          "--evidence", "re-measured after the BIOS flash", "--supersedes", base])
    out = graph_mod.citations(cfg, base)
    assert later not in out["cites"]
    assert out["cited_by"] != [], "the correcting entry cites the one it supersedes in its edges"


def test_a_cited_id_in_prose_is_a_citation(tmp_path):
    vault, base, _later, _unrelated = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    main(["--vault", str(vault), "write", "--summary",
          "The divider findings were confirmed twice, see " + base + " for the boot evidence",
          "--state", "raw", "--kind", "fact", "--project", "wraith"])
    out = graph_mod.citations(cfg, base)
    assert len(out["cited_by"]) == 1


def test_citations_write_nothing(tmp_path):
    """A derived view that drifts is worse than the gap it fills."""
    vault, base, _later, _unrelated = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    before = _files(vault)
    graph_mod.citations(cfg, base)
    graph_mod.neighbors(cfg, base)
    assert _files(vault) == before


def test_neighbours_answers_for_an_entry_id(tmp_path, capsys):
    vault, base, _later, _unrelated = _seed(tmp_path)
    capsys.readouterr()
    assert main(["--vault", str(vault), "neighbors", base]) == 0
    report = graph_mod.neighbors(sigil.config.load(vault), base)
    assert report["citations"]["entry"] == base


def test_audit_flags_a_predicate_outside_the_vocabulary(tmp_path, capsys):
    vault, _base, _later, _unrelated = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    graph_mod.add_edge(cfg, "quipu-vault", "part_of", "quipu")
    graph_mod.add_edge(cfg, "quipu-vault", "served_by", "quipu-mcp", allow_new_predicate=True)
    capsys.readouterr()
    main(["--vault", str(vault), "audit"])
    out = capsys.readouterr().out
    assert "served_by" in out and "closed" in out, out
    assert "part_of" not in out.split("served_by")[0].split("problems")[-1]


def test_a_clean_vault_audits_clean(tmp_path, capsys):
    vault, _base, _later, _unrelated = _seed(tmp_path)
    capsys.readouterr()
    assert main(["--vault", str(vault), "audit"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):]) if "{" in out else {}
    assert report.get("problems") == [], out
