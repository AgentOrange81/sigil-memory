"""A project hub is a view over the tag, not 466 persisted edges (#85).

The measurement that filed the issue: 891 entries, 3 edges, 466 entries carrying a real project tag.
The obvious fill — one `part_of` edge per tagged entry — would mean writing 466 edge specs into 466
markdown files, and then `graph.rebuild` (which folds db edges back into the markdown) would keep
rewriting them on every reindex. The tag already IS the assertion and it is already in the markdown.

So the hub is derived at read time. These tests pin the two things that could go wrong with a derived
view: that it stays honest (identity matching, no `unknown` hub) and that it writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import sigil.config
import sigil.index
from sigil import graph as graph_mod
from sigil.cli import main

SHITTER = "Shitter's docker build was fixed by pinning the builder stage to node 22"
GOONIFY = "goonify.fun routes mail through Cloudflare Email Routing, not a mailbox provider"
QUIPU = "quipu 0.4.0 shipped the Hermes memory provider"


def _seed(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    sigil.config.load(vault, create=True)
    for summary, project in ((SHITTER, "shitter"), (GOONIFY, "goonify"), (QUIPU, "quipu"),
                             ("An unlabelled observation about something", "unknown")):
        main(["--vault", str(vault), "write", "--summary", summary, "--state", "promoted",
              "--project", project, "--importance", "0.9"])
    return vault


def _files(vault: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in (vault / "notes").glob("*.md")}


def test_the_hub_lists_the_entries_tagged_with_that_project(tmp_path):
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    rows = graph_mod.project_entries(cfg, "shitter")
    assert [r["summary"] for r in rows] == [SHITTER]


def test_the_hub_is_identity_matched(tmp_path):
    """`Shitter` and `shitter` are one project — the same identity rule the graph uses for entities."""
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    assert graph_mod.project_entries(cfg, "Shitter") == graph_mod.project_entries(cfg, "shitter")


def test_unknown_is_not_a_hub(tmp_path):
    """425 entries on the live vault carry `unknown`; a hub for it would be a bucket, not a claim."""
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    assert graph_mod.project_entries(cfg, "unknown") == []


def test_neighbours_carries_the_hub_and_still_answers_for_a_non_project(tmp_path):
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    graph_mod.add_edge(cfg, "quipu-vault", "part_of", "quipu")
    out = graph_mod.neighbors(cfg, "quipu")
    assert out["edges"], "the asserted edge is still there"
    assert [r["summary"] for r in out["entries"]] == [QUIPU], "and the tag view is alongside it"
    assert graph_mod.neighbors(cfg, "quipu-vault")["entries"] == []


def test_the_hub_writes_nothing(tmp_path):
    """The whole point of a view: no edge rows, no markdown churn, nothing for rebuild to fold."""
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    before_text = _files(vault)
    c = sigil.index.connect(cfg.db_path)
    edges_before = c.execute("select count(*) from edges").fetchone()[0]
    c.close()
    graph_mod.project_entries(cfg, "shitter")
    graph_mod.neighbors(cfg, "shitter")
    c = sigil.index.connect(cfg.db_path)
    edges_after = c.execute("select count(*) from edges").fetchone()[0]
    c.close()
    assert edges_after == edges_before == 0
    assert _files(vault) == before_text


def test_pruned_entries_are_left_out_of_the_hub(tmp_path):
    """Pruning is the vault deciding something is not knowledge; a browse surface must not lead with
    rejected material, and on the live vault the pruned shitter entries are transcript-summary junk."""
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    pruned = [r for r in graph_mod.project_entries(cfg, "shitter")]
    assert pruned, "the seeded entry is there before pruning"
    path = next((vault / "notes").glob("*.md")).parent
    for p in path.glob("*.md"):
        text = p.read_text()
        if SHITTER in text:
            p.write_text(text.replace("state: promoted", "state: pruned"))
    assert graph_mod.project_entries(cfg, "shitter") == []
    assert len(graph_mod.project_entries(cfg, "shitter", include_pruned=True)) == 1


def test_cli_neighbours_prints_entries_not_edges(tmp_path, capsys):
    vault = _seed(tmp_path)
    capsys.readouterr()
    assert main(["--vault", str(vault), "neighbors", "goonify"]) == 0
    out = capsys.readouterr().out
    assert "entry " in out and GOONIFY[:40] in out
    assert " -part_of-> " not in out, "the view must not pretend to be an asserted edge"


def test_cli_timeline_answers_for_a_project_tag(tmp_path, capsys):
    vault = _seed(tmp_path)
    capsys.readouterr()
    assert main(["--vault", str(vault), "timeline", "shitter"]) == 0
    out = capsys.readouterr().out
    assert "entry " in out and SHITTER[:40] in out


def test_stats_reports_connectivity_and_hubs(tmp_path, capsys):
    vault = _seed(tmp_path)
    cfg = sigil.config.load(vault)
    graph_mod.add_edge(cfg, "quipu-vault", "part_of", "quipu")
    capsys.readouterr()
    assert main(["--vault", str(vault), "stats"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):]) if "{" in out else json.loads(out)
    assert report["edges_by_predicate"] == {"part_of": 1}
    assert report["graph_nodes"] == 2
    assert report["entries_with_an_edge"] == 0, "an edge asserted by hand has no note behind it"
    assert report["projects"]["shitter"] == 1
    assert "unknown" not in report["projects"]
