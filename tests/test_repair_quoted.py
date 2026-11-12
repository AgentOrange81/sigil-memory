"""`quipu repair` — rewriting the 88 quoted summaries without breaking anything (#58, from #54).

The repair has two properties worth more than its output: it never guesses, and it never renames. A note
whose source cannot be found is reported and left alone, because a repaired parser does not repair a
vault and a wrong summary is worse than a quoted one. Nothing here touches the index until the notes are
written, and the write is in place — the file keeps its path, so no duplicate appears under a new slug.
"""

import textwrap

import pytest

from sigil import repair as repair_mod
from sigil.cli import main
from sigil.store import iter_entries

TRUNCATED = "'Corrected metadata for the PRL fleet (agent: hermes, project_id:"
FULL = "Corrected metadata for the PRL fleet (agent: hermes, project_id: prl-mining)"
GLITCH_TRUNC = "'Glitch migration complete: containers rebuilt and the tunnel"
GLITCH_FULL = "Glitch migration complete: containers rebuilt and the tunnel reconnected"


def write_note(root, eid, summary_line, body_lines, state="promoted", kind="decision"):
    """Raw markdown, not `write_entry`: the defect lives in notes the IMPORTER wrote, and their shape
    (quoted, truncated, repeated into an H1 and a paragraph) is what the repair has to survive."""
    path = root / "notes" / f"{eid}-{eid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = textwrap.dedent(f"""\
        ---
        id: {eid}
        kind: {kind}
        state: {state}
        created: 2026-10-01T10:00:00Z
        updated: 2026-10-01T10:00:00Z
        summary: "{summary_line}"
        evidence: "x"
        ---

        """)
    path.write_text(head + "\n".join(body_lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    # (a) recoverable from the fabric origin: the body only repeats the truncated text
    write_note(root, "aaaa1111", TRUNCATED, [f"# {TRUNCATED}", TRUNCATED, "**Evidence:** x"])
    # (b) no fabric origin: the body carries the continuation the wrap cut off
    write_note(root, "bbbb2222", GLITCH_TRUNC,
               [f"# {GLITCH_TRUNC}", f"'{GLITCH_FULL}'", "**Evidence:** x"])
    # (c) quoted, truncated, and the body says nothing new -> must not be guessed at
    write_note(root, "cccc3333", "'a quoted summary with no continuation anywhere",
               ["# 'a quoted summary with no continuation anywhere", "**Evidence:** x"], state="raw")
    # (d) already clean
    write_note(root, "dddd4444", "A clean summary that needs nothing", ["body"])
    fabric = tmp_path / "fabric"
    fabric.mkdir()
    (fabric / "agent-resolution-corrected-metadata-prl-0573.md").write_text(textwrap.dedent("""
        ---
        id: aaaa1111
        agent: hermes
        type: resolution
        summary: 'Corrected metadata for the PRL fleet (agent: hermes, project_id:
          prl-mining)'
        ---

        body
    """).lstrip("\n"), encoding="utf-8")
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    return root, fabric


def summaries(root):
    return {e.id: e.summary for _p, e in iter_entries(root)}


def test_dry_run_reports_and_writes_nothing(vault, capsys):
    root, fabric = vault
    before = {p.name: p.read_text() for p in (root / "notes").glob("*.md")}
    assert repair_mod.find(root, fabric).repairs
    after = {p.name: p.read_text() for p in (root / "notes").glob("*.md")}
    assert before == after, "a dry run wrote to the vault"


def test_a_repair_from_the_fabric_origin_restores_the_folded_summary(vault):
    root, fabric = vault
    repair_mod.apply(root, repair_mod.find(root, fabric))
    assert summaries(root)["aaaa1111"] == FULL


def test_a_repair_from_the_body_uses_the_line_the_import_duplicated(vault):
    root, fabric = vault
    repair_mod.apply(root, repair_mod.find(root, fabric))
    assert summaries(root)["bbbb2222"] == GLITCH_FULL


def test_an_unrecoverable_note_is_reported_and_left_exactly_as_it_was(vault):
    root, fabric = vault
    result = repair_mod.find(root, fabric)
    bad = [r for r in result.repairs if r.id == "cccc3333"]
    assert bad and bad[0].reason and not bad[0].new, bad
    repair_mod.apply(root, result)
    assert summaries(root)["cccc3333"] == "'a quoted summary with no continuation anywhere", \
        "a repair that guesses is worse than a note that stays wrong"


def test_a_clean_entry_is_not_touched_and_the_path_does_not_move(vault):
    root, fabric = vault
    paths = {p.name: p for p in (root / "notes").glob("*.md")}
    repair_mod.apply(root, repair_mod.find(root, fabric))
    assert summaries(root)["dddd4444"] == "A clean summary that needs nothing"
    assert sorted(p.name for p in (root / "notes").glob("*.md")) == sorted(paths), \
        "the repair renamed a note instead of editing it in place"


def test_the_body_copy_of_the_old_summary_is_replaced_too(vault):
    root, fabric = vault
    repair_mod.apply(root, repair_mod.find(root, fabric))
    path = next(p for p in (root / "notes").glob("aaaa1111-*.md"))
    text = path.read_text()
    assert TRUNCATED not in text, "the truncated copy is still in the file (frontmatter or body)"
    assert "project_id: prl-mining" in text, "the repaired summary is not in the file"


def test_the_repair_is_idempotent(vault):
    root, fabric = vault
    repair_mod.apply(root, repair_mod.find(root, fabric))
    again = repair_mod.find(root, fabric)
    fixed = [r for r in again.repairs if not r.reason]
    assert fixed == [], f"still repairable after a repair: {fixed}"


def test_cli_exits_nonzero_while_anything_is_unresolved(vault, capsys):
    root, fabric = vault
    assert main(["--vault", str(root), "repair", "--apply", "--source", str(fabric)]) == 1
    out = capsys.readouterr().out
    assert '"unresolved": 1' in out, out


def test_cli_apply_reports_what_it_wrote(vault, capsys):
    root, fabric = vault
    assert main(["--vault", str(root), "repair", "--apply", "--source", str(fabric)]) in (0, 1)
    out = capsys.readouterr().out
    assert '"rewritten": 2' in out, out
