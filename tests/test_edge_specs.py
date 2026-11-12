"""An edge spec the replay cannot assert must be reported by `audit`, not only by `reindex` (#98).

`reindex` prints one skip line per spec it cannot assert and keeps going — correct, a broken claim
must not cost the memory. `audit` then called the vault clean, because its predicate check reads the
`edges` TABLE and a skipped spec never reaches a row. On the live vault that is three assertions an
operator wrote, unqueryable, with the reporting surface silent about them:

    wraith:kernel-7.0.0-43:required:nvidia-driver-615-open   <- four parts, silently re-shaped
    75771220:supported_by:this                                <- predicate outside the vocabulary
    ee066f78:extends:1ffca549986a4928b3                        <- same

The four-part one is the nastier shape: `split(":", 2)` shoves the remainder into `dst`, so
`s:kernel-7.0.0-43:required:driver` parses "fine" as src=s, predicate=kernel-7.0.0-43,
dst=required:driver — a spec that means something nobody wrote, and that no writer can re-emit.

Fixtures are RAW markdown, never `write_entry` output: the store rewrites the frontmatter on the way
in, so a fixture built through the writer cannot reproduce the file an older version left behind.

QA scope: tests/ only — the builder owns quipu/**.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sigil.store import parse_edge_spec

NOTE = """---
id: 0badf00d
kind: conclusion
state: promoted
agent: glitch
project: quipu
created: 2026-11-01T00:00:00Z
updated: 2026-11-01T00:00:00Z
confidence: 0.5
importance: 0.9
summary: "a claim whose edge spec the replay cannot assert"
evidence: "hand-written fixture"
{edges}---

# a claim whose edge spec the replay cannot assert
"""


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "sigil", *args], capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    assert run_cli("--vault", str(v), "init").returncode == 0
    return v


def plant(vault, edges: str):
    (Path(vault) / "notes").mkdir(parents=True, exist_ok=True)
    (Path(vault) / "notes" / "0badf00d-a-claim.md").write_text(
        NOTE.format(edges=edges), encoding="utf-8")


def test_parse_refuses_a_four_part_spec():
    with pytest.raises(ValueError) as exc:
        parse_edge_spec("wraith:kernel-7.0.0-43:required:nvidia-driver-615-open")
    assert "4" in str(exc.value) or "four" in str(exc.value).lower(), str(exc.value)


def test_parse_still_accepts_three_parts():
    assert parse_edge_spec("quipu:depends_on:sqlite") == ("quipu", "depends_on", "sqlite")


def test_audit_reports_a_spec_the_replay_cannot_assert(vault):
    plant(vault, 'edges: ["75771220:supported_by:this"]\n')
    proc = run_cli("--vault", str(vault), "audit")
    assert proc.returncode == 1, "an unassertable spec is a problem, not a clean vault"
    report = json.loads(proc.stdout)
    joined = " | ".join(report["problems"])
    assert "0badf00d" in joined and "supported_by" in joined, joined


def test_audit_reports_a_malformed_spec_as_malformed(vault):
    plant(vault, 'edges: ["wraith:kernel-7.0.0-43:required:nvidia-driver-615-open"]\n')
    report = json.loads(run_cli("--vault", str(vault), "audit").stdout)
    joined = " | ".join(report["problems"])
    assert "0badf00d" in joined and "malformed" in joined.lower(), joined


def test_audit_and_reindex_agree_on_the_count(vault):
    """The skip line and the audit must count the same specs — one surface saying 3 and the other 0 is
    the defect this issue is about."""
    plant(vault, 'edges: ["a:invented_one:b", "c:depends_on:d", "e:f:g:h"]\n')
    proc = run_cli("--vault", str(vault), "reindex")
    assert proc.returncode == 0, proc.stderr
    skipped = proc.stdout.count("skipped:") + proc.stderr.count("skipped:")
    assert skipped == 2, f"expected 2 unassertable specs, reindex said {skipped}: {proc.stdout}{proc.stderr}"
    problems = json.loads(run_cli("--vault", str(vault), "audit").stdout)["problems"]
    assert len(problems) >= skipped, problems


def test_mcp_write_skips_a_four_part_spec_and_keeps_the_entry(tmp_path):
    from sigil import config as config_mod
    from sigil import mcp as mcp_mod

    cfg = config_mod.load(tmp_path / "vault", create=True)
    result = mcp_mod._write(cfg, {
        "summary": "a memory whose edge spec has four parts",
        "evidence": "fixture",
        "importance": 0.9,
        "edges": ["a:b:c:d"],
    })
    assert result["skipped_edges"], "a four-part spec is a broken claim, not a silent reshape"
    text = Path(result["path"]).read_text()
    assert "a:b:c:d" in text, "the claim stays in the markdown — only its assertion is refused"
