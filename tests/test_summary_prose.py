"""A summary is a statement, not a blob (#77).

The defect: `write`, `capture` and the fabric importer all accepted a summary that opened with `{`,
`[` or a code fence. A blob's tokens index exactly as well as prose's, so twenty-one entries on the
live vault were ranking on text no agent could use — inside a 400-token recall budget. The write
boundary now refuses the shape; the importer derives a statement from the body instead, because a
historical corpus should not lose a memory to tidy a field; and `audit` reports anything that got in
before the rule existed.

Everything here goes through the public surfaces (`sigil.cli.main`, `promote.record_capture`, the
importer) rather than the helpers, so the tests fail if a caller stops applying the rule.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import sigil.config
import sigil.index
from sigil import promote
from sigil.cli import main
from sigil.importers.fabric import import_fabric
from sigil.store import iter_entries, looks_like_blob

BLOB = '{"corrections_verified": [ {"correction": "(a) start() adds NO listeners"}]}'
PROSE = "The release wheel refused to install until the filename kept all five parts"


def _entries(vault: Path) -> list:
    """The vault's entries, in disk order — `iter_entries` yields (path, entry) pairs."""
    return [entry for _path, entry in iter_entries(vault)]


def _vault(tmp_path: Path) -> Path:
    sigil.config.load(tmp_path / "vault", create=True)
    return tmp_path / "vault"


def test_the_shape_is_what_we_think_it_is():
    assert looks_like_blob(BLOB)
    assert looks_like_blob("```json\n{}\n```")
    assert looks_like_blob('[1, 2, 3]')
    assert looks_like_blob(f'"{BLOB}"')  # a quoted YAML scalar of the same blob
    assert not looks_like_blob(PROSE)
    assert not looks_like_blob("")


def test_write_refuses_a_blob_summary_with_exit_2(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    rc = main(["--vault", str(vault), "write", "--summary", BLOB, "--state", "raw"])
    err = capsys.readouterr().err
    assert rc == 2, "a blob summary is a refusal, not a warning"
    assert "serialised blob" in err
    assert _entries(vault) == [], "nothing may be written when the handle is refused"


def test_write_accepts_the_same_payload_with_the_opt_out(tmp_path):
    vault = _vault(tmp_path)
    rc = main(["--vault", str(vault), "write", "--summary", BLOB, "--state", "raw",
               "--allow-summary-blob"])
    assert rc == 0
    assert [e.summary for e in _entries(vault)] == [BLOB]


def test_capture_takes_the_first_prose_line_not_the_first_line(tmp_path):
    vault = _vault(tmp_path)
    entry = promote.record_capture(sigil.config.load(vault), BLOB + "\n" + PROSE)
    assert entry.summary == PROSE, entry.summary
    assert BLOB in entry.body, "the blob is evidence; it belongs in the body, not the handle"


def test_capture_refuses_a_body_with_no_prose_at_all(tmp_path):
    vault = _vault(tmp_path)
    cfg = sigil.config.load(vault)
    try:
        promote.record_capture(cfg, BLOB)
    except ValueError as exc:
        assert "serialised blob" in str(exc)
    else:  # pragma: no cover - the failure this test exists for
        raise AssertionError("a capture with no statement anywhere was accepted")
    assert _entries(vault) == []


def _fabric_entry(path: Path, summary: str, body: str) -> None:
    path.write_text(
        "---\n"
        f'summary: "{summary}"\n'
        'type: "decision"\n'
        'training_value: "high"\n'
        'evidence: "measured"\n'
        "---\n\n"
        f"{body}\n"
    )


def test_importer_derives_a_statement_from_the_body(tmp_path):
    src = tmp_path / "corpus"
    src.mkdir()
    _fabric_entry(src / "blob.md", BLOB, "# Heading\n\n" + PROSE)
    vault = _vault(tmp_path)
    result = import_fabric(src, vault, promote_threshold=0.5)
    assert len(result.written) == 1
    (entry,) = _entries(vault)
    assert entry.summary == PROSE
    assert "summary not prose" not in {reason for _p, reason in result.skipped}


def test_importer_refuses_an_entry_with_no_prose_anywhere(tmp_path):
    src = tmp_path / "corpus"
    src.mkdir()
    _fabric_entry(src / "blob.md", BLOB, BLOB)
    vault = _vault(tmp_path)
    result = import_fabric(src, vault, promote_threshold=0.5)
    assert result.written == []
    assert [reason for _p, reason in result.skipped] == ["summary not prose"]
    assert _entries(vault) == []


def test_audit_reports_a_blob_already_in_the_vault(tmp_path, capsys):
    """A vault imported before the rule can be triaged rather than read end to end."""
    vault = _vault(tmp_path)
    rc = main(["--vault", str(vault), "write", "--summary", BLOB, "--state", "raw",
               "--allow-summary-blob"])
    assert rc == 0
    capsys.readouterr()
    main(["--vault", str(vault), "audit"])
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{"):]) if "{" in out else {}
    text = out + json.dumps(report)
    assert "blob summary" in text, out


def test_the_prose_rule_does_not_touch_a_normal_summary(tmp_path):
    vault = _vault(tmp_path)
    assert main(["--vault", str(vault), "write", "--summary", PROSE, "--state", "raw"]) == 0
    assert [e.summary for e in _entries(vault)] == [PROSE]


def test_stdin_body_capture_is_not_mistaken_for_a_summary(tmp_path, monkeypatch):
    """`capture` reads the body from stdin; a JSON body there must not become the handle."""
    vault = _vault(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(BLOB + "\n" + PROSE + "\n"))
    rc = main(["--vault", str(vault), "capture"])
    assert rc == 0
    (entry,) = _entries(vault)
    assert entry.summary == PROSE
