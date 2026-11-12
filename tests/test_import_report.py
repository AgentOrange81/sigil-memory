"""Issue #17 QA: the import report's reasons and ignored directories.

Contract tests for `quipu import fabric` (spec-m1-vector.md). The report is one
JSON document on stdout carrying, beyond the counts that already exist:

    skipped_by_reason    {reason: count} over exactly these strings:
                         "ignored name", "empty body", "no type",
                         "unparsable frontmatter", "duplicate id",
                         "noise (training_value: low)"
    directories_ignored  {direct-subdirectory: count of *.md at any depth} for
                         every direct subdirectory holding at least one *.md;
                         empty and .git-like directories are omitted, and
                         subdirectories are never imported.

Fixtures live in tmp_path; no network, no sleeps.
"""
import json

from sigil.cli import main
from sigil.store import iter_entries

#: the report's exact top-level key set
REPORT_KEYS = {"source", "vault", "dry_run", "written", "skipped",
               "skipped_by_reason", "directories_ignored", "by_kind", "by_state"}

NOISE_REASON = "noise (training_value: low)"

#: one file per skip reason; every fixture is valid apart from the one thing
#: that disqualifies it, so each reason fires for exactly one file
REASON_CORPUS = {
    # a top-level *.md counted and skipped because of its name
    "README.md": "placeholder",
    # valid frontmatter, no body
    "empty-body.md": "placeholder",
    # valid frontmatter but no `type` key at all
    "no-type.md": "placeholder",
    # no frontmatter fences: nothing to parse
    "unparsable.md": "plain prose, no frontmatter\nsecond line\n",
    # two files sharing one id: the first claimant is written, the second is
    # the duplicate (import_fabric claims ids first-come on the sorted glob)
    "dup-a.md": "placeholder",
    "dup-b.md": "placeholder",
    # session + low training value: the corpus's own noise rule
    "noise-low.md": "placeholder",
}

#: the report's skipped_by_reason for REASON_CORPUS, exact
EXPECTED_REASONS = {
    "ignored name": 1,
    "empty body": 1,
    "no type": 1,
    "unparsable frontmatter": 1,
    NOISE_REASON: 1,
}


def _entry(fid, *, type: str | None = "decision", training_value="high", summary="a summary",
           body=None, outcome="what happened", evidence="pytest -q: ok"):
    """One synthetic fabric file, valid enough to be written when nothing else disqualifies it."""
    lines = ["---"]
    if fid is not None:
        lines.append(f'id: "{fid}"')
    lines += [
        'agent: "vector"',
        'platform: "cli"',
        'timestamp: "2026-10-01T12:00:00Z"',
    ]
    if type is not None:
        lines.append(f'type: "{type}"')
    lines += [
        'tier: "hot"',
        f"summary: {json.dumps(summary)}",
        'project_id: "quipu"',
        'status: "completed"',
        f'outcome: "{outcome}"',
        f'evidence: "{evidence}"',
        f'training_value: "{training_value}"',
        "---",
    ]
    if body is None:
        body = "the body of the entry"
    if body:
        lines += ["", body]
    return "\n".join(lines) + "\n"


def _build_corpus():
    """REASON_CORPUS with real content: each file valid apart from its one disqualifier."""
    corpus = dict(REASON_CORPUS)
    corpus["README.md"] = _entry("read0001", summary="A readme is docs, not a claim")
    corpus["empty-body.md"] = _entry("empty001", summary="Empty body is not evidence", body="")
    corpus["no-type.md"] = _entry("notype01", summary="A missing type has no kind",
                                  type=None, training_value="high")
    corpus["dup-a.md"] = _entry("dup00001", summary="first duplicate")
    corpus["dup-b.md"] = _entry("dup00001", type="lesson", summary="second duplicate")
    corpus["noise-low.md"] = _entry("noise001", type="session", training_value="low",
                                    summary="Noise: one skipped session")
    return corpus


def _write(tmp_path, files, name="fabric"):
    """Write {relative name: text} under tmp_path/name, creating subdirectories."""
    src = tmp_path / name
    src.mkdir(exist_ok=True)  # a second run in the same test reuses the corpus directory
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return src


def _report(tmp_path, capsys, files, name="fabric", vault_name="vault", extra=()):
    """Run `quipu import fabric` through the CLI and return (report, src, vault)."""
    src = _write(tmp_path, files, name)
    vault = tmp_path / vault_name
    code = main(["--vault", str(vault), "import", "fabric", str(src), *extra])
    out = capsys.readouterr().out
    assert code == 0
    return json.loads(out), src, vault


def test_good_import_full_shape(tmp_path, capsys):
    """Two valid entries: written == 2 and the empty dicts, plus the exact key set.

    An empty subdirectory and a `.git/settings.md` are ignored-directory
    non-candidates: empty and .git-like directories are omitted.
    """
    files = {
        "good-1.md": _entry("good0001", summary="first good entry"),
        "good-2.md": _entry("good0002", summary="second good entry"),
        ".git/settings.md": _entry("gitconf1", summary="config, not memory"),
    }
    src = _write(tmp_path, files)
    (src / "_empty").mkdir()
    vault = tmp_path / "vault"

    code = main(["--vault", str(vault), "import", "fabric", str(src)])
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)

    assert report == {
        "source": str(src),
        "vault": str(vault),
        "dry_run": False,
        "written": 2,
        "skipped": 0,
        "skipped_by_reason": {},
        "directories_ignored": {},
        "by_kind": {"decision": 2},
        "by_state": {"promoted": 2},
    }
    assert set(report) == REPORT_KEYS
    assert len(list(iter_entries(vault))) == 2


def test_every_reason_appears_once(tmp_path, capsys):
    """Seven files, one per skip reason: the exact skipped_by_reason dict.

    Only the first duplicate claimant is written (reason books charge the loser).
    """
    report, src, vault = _report(tmp_path, capsys, _build_corpus())

    assert report["skipped_by_reason"] == EXPECTED_REASONS
    # the two files that share an id are BOTH written, the loser under a path-hashed id: dropping a
    # file because a neighbour took its id loses data for the sake of a tidier report (#17)
    assert report["written"] == 2
    summaries = {e.summary for _p, e in iter_entries(vault)}
    assert summaries == {"first duplicate", "second duplicate"}


def test_skipped_sums_to_the_report_and_files_considered(tmp_path, capsys):
    """skipped == sum(skipped_by_reason.values()), and the books balance:
    written + skipped == every top-level *.md considered (subdirectories are
    never candidates)."""
    report, src, _vault = _report(tmp_path, capsys, _build_corpus())

    reasons = report["skipped_by_reason"]
    assert report["skipped"] == sum(reasons.values())
    considered = len(list(src.glob("*.md")))
    assert considered == 7  # the fixture's own size: README counts, subdirectories do not
    assert report["written"] + report["skipped"] == considered


def test_ignored_directories_counted_at_depth(tmp_path, capsys):
    """`_stubs/a.md`, `_stubs/nested/b.md`, `notes/c.md` -> {"_stubs": 2, "notes": 1};
    written."""
    files = {
        "top-1.md": _entry("top00001", summary="the only top-level entry"),
        "_stubs/a.md": _entry("stuba001", summary="stub at the first level"),
        "_stubs/nested/b.md": _entry("stubb001", summary="stub at depth two"),
        "notes/c.md": _entry("notesc1", summary="note markdown"),
    }
    report, src, vault = _report(tmp_path, capsys, files)

    assert report["directories_ignored"] == {"_stubs": 2, "notes": 1}
    assert report["written"] == 1
    assert report["skipped"] == 0
    assert report["skipped_by_reason"] == {}
    summaries = {e.summary for _p, e in iter_entries(vault)}
    assert summaries == {"the only top-level entry"}


def test_include_noise_drops_the_noise_reason(tmp_path, capsys):
    """--include-noise: the noise reason leaves skipped_by_reason and the entry
    is written instead."""
    corpus = _build_corpus()
    report, _src, _vault = _report(tmp_path, capsys, corpus)

    assert report["skipped_by_reason"][NOISE_REASON] == 1

    noisy, _src, noisy_vault = _report(tmp_path, capsys, corpus, vault_name="noisy-vault",
                                       extra=["--include-noise"])
    assert NOISE_REASON not in noisy["skipped_by_reason"]
    assert noisy["skipped_by_reason"] == {k: v for k, v in EXPECTED_REASONS.items()
                                          if k != NOISE_REASON}
    assert noisy["written"] == 3
    assert noisy["skipped"] == 4
    summaries = {e.summary for _p, e in iter_entries(noisy_vault)}
    assert summaries == {"first duplicate", "second duplicate", "Noise: one skipped session"}


def test_dry_run_writes_nothing_and_reports_the_same_shape(tmp_path, capsys):
    """--dry-run prints the same document (minus dry_run) and writes nothing."""
    corpus = _build_corpus()
    src = _write(tmp_path, corpus)
    vault = tmp_path / "vault"

    code = main(["--vault", str(vault), "import", "fabric", str(src), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    dry = json.loads(out)
    assert not vault.exists() or not list(vault.rglob("*.md"))

    code = main(["--vault", str(vault), "import", "fabric", str(src)])
    out = capsys.readouterr().out
    assert code == 0
    real = json.loads(out)

    assert dry.pop("dry_run") is True
    assert real.pop("dry_run") is False
    assert dry == real
    assert dry["skipped_by_reason"] == EXPECTED_REASONS
    assert dry["directories_ignored"] == {}

    # the real run really wrote, so the identical shape is not vacuous emptiness
    assert len(list(iter_entries(vault))) == 2


def test_non_markdown_top_level_is_not_counted(tmp_path, capsys):
    """A *.txt (and any other non-markdown) file at the top level is counted
    nowhere: not written, not skipped, not by_kind, not a directory."""
    files = {
        "entry.md": _entry("entry001", summary="the one real entry"),
        "notes.txt": "some notes\nnot markdown\n",
        "data.json": json.dumps({"just": "data"}),
    }
    report, src, vault = _report(tmp_path, capsys, files)

    assert report == {
        "source": str(src),
        "vault": str(vault),
        "dry_run": False,
        "written": 1,
        "skipped": 0,
        "skipped_by_reason": {},
        "directories_ignored": {},
        "by_kind": {"decision": 1},
        "by_state": {"promoted": 1},
    }
    assert set(report) == REPORT_KEYS
    assert len(list(iter_entries(vault))) == 1

def test_recurse_reads_nested_entries_and_still_refuses_the_scaffolding(tmp_path, capsys):
    """--recurse imports a nested corpus but never _stubs/_dupes/templates, and the report
    stops claiming those trees were 'left behind' because they were walked and refused."""
    files = {
        "top.md": _entry("top00001", summary="a top-level entry"),
        "projects/deep.md": _entry("deep0001", summary="a nested entry"),
        "_stubs/a.md": _entry("stuba001", summary="a stub"),
        "_dupes/b.md": _entry("dupes001", summary="a duplicate copy"),
        "templates/t.md": _entry("tmpl0001", summary="a template"),
    }
    report, src, vault = _report(tmp_path, capsys, files, extra=["--recurse"])

    assert report["written"] == 2
    assert report["directories_ignored"] == {}
    summaries = {e.summary for _p, e in iter_entries(vault)}
    assert summaries == {"a top-level entry", "a nested entry"}


def test_recurse_with_a_custom_skip_dir_refuses_the_named_tree_only(tmp_path, capsys):
    """--skip-dir replaces the default refusal list, so a corpus whose scaffolding lives under a
    different name can be imported without a code change (the issue's second requirement)."""
    files = {
        "top.md": _entry("top00001", summary="a top-level entry"),
        "projects/deep.md": _entry("deep0001", summary="a nested entry"),
        "_stubs/a.md": _entry("stuba001", summary="a stub"),
    }
    report, src, vault = _report(tmp_path, capsys, files, extra=["--recurse", "--skip-dir", "projects"])

    assert report["written"] == 2
    summaries = {e.summary for _p, e in iter_entries(vault)}
    assert summaries == {"a top-level entry", "a stub"}
