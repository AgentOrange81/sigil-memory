"""Import a synthetic fabric corpus into a vault.

The fixtures live in tmp_path: no real corpus, no committed vault.
"""
import json

from sigil.cli import main
from sigil.importers.fabric import import_fabric
from sigil.store import entry_path, iter_entries


def _fabric_entry(fid, *, type="code-session", training_value="high", summary="a summary",
                  agent="cipher", project="quipu", outcome="", evidence="", tags=None,
                  timestamp="2026-10-01T12:00:00Z", body=None):
    """One synthetic fabric file, as the Icarus corpus writes them."""
    lines = ["---"]
    if fid is not None:
        lines.append(f'id: "{fid}"')
    lines += [
        f'agent: "{agent}"',
        'platform: "cli"',
        f'timestamp: "{timestamp}"',
        f'type: "{type}"',
        'tier: "hot"',
        # json.dumps escapes non-ASCII, so a summary with an em dash lands in the
        # frontmatter as a literal \u2014 escape -- exactly what the corpus does.
        f'summary: {json.dumps(summary)}',
        f'project_id: "{project}"',
        'status: "completed"',
    ]
    if outcome:
        lines.append(f'outcome: {json.dumps(outcome)}')
    if evidence:
        lines.append(f'evidence: {json.dumps(evidence)}')
    if training_value is not None:
        lines.append(f'training_value: "{training_value}"')
    if tags:
        lines.append("tags: [" + ", ".join(tags) + "]")
    if body is None:
        body = "the body of the entry"
    lines += ["---", "", body, ""]
    return "\n".join(lines)


# 12 files: every kind the mapping knows, every state it can produce, the noise
# rule (session/code-session + low training value), an unknown type, and a body
# that repeats its own H1/summary preamble.
CORPUS = {
    # name: (entry kwargs)
    "01-decision.md": dict(fid="aaaa0001", type="decision", training_value="high",
                            summary="Use json.loads for the frontmatter, not a regex",
                            outcome="the parser stopped truncating quotes",
                            evidence="pytest -q: 5 passed"),
    "02-lesson.md": dict(fid="aaaa0002", type="lesson", training_value="normal",
                          summary="A body is not a summary: keep them apart",
                          outcome="entries carry their own body",
                          evidence="quipu show 02-lesson"),
    "03-fact.md": dict(fid="aaaa0003", type="fact", training_value="high",
                        summary="The corpus holds 5469 entries and 84% are session noise",
                        outcome="the noise ratio was measured",
                        evidence="ls ~/fabric | wc -l"),
    "04-handoff.md": dict(fid="aaaa0004", type="handoff", training_value="high",
                           summary="Hand the corpus to the importer, not to a human",
                           outcome="the import is scripted", evidence="issue #1"),
    "05-conclusion.md": dict(fid="aaaa0005", type="conclusion", training_value="high",
                              summary="Fabric frontmatter is JSON-ish, not YAML",
                              outcome="json.loads handles the escapes",
                              evidence="json.loads('\"\\u2014\"')"),
    "06-question.md": dict(fid="aaaa0006", type="question", training_value="normal",
                            summary="Should the importer write pruned entries at all?",
                            outcome="yes, as a negative training example",
                            evidence="quipu export dpo"),
    "07-session-high.md": dict(fid="aaaa0007", type="session", training_value="high",
                                summary="A high-value session is real memory",
                                outcome="imported as a capture", evidence="session id abc"),
    "08-code-session-normal.md": dict(fid="aaaa0008", type="code-session",
                                       training_value="normal",
                                       summary="A normal code session is worth keeping",
                                       outcome="imported as a capture", evidence="tsc 0"),
    "09-research.md": dict(fid="aaaa0009", type="research", training_value="high",
                            summary="An unknown type falls back to conclusion",
                            outcome="mapped to conclusion", evidence="issue #1"),
    "10-decision-low.md": dict(fid="aaaa0010", type="decision", training_value="low",
                                summary="A pruned decision is a negative training example",
                                outcome="kept, but pruned", evidence="issue #1"),
    "11-session-low.md": dict(fid="aaaa0011", type="session", training_value="low",
                               summary="Noise: read a file and closed it",
                               outcome="nothing happened", evidence="no output"),
    "12-code-session-raw.md": dict(fid="aaaa0012", type="code-session", training_value=None,
                                    summary="No training value means raw, not promoted",
                                    outcome="left raw for a later reflection pass",
                                    evidence="no evidence",
                                    body="# No training value means raw, not promoted\n\n"
                                         "No training value means raw, not promoted\n\n"
                                         "the body of the entry"),
}


def _write_corpus(tmp_path, extra=None, name="fabric"):
    src = tmp_path / name
    src.mkdir()
    files = dict(CORPUS)
    if extra:
        files.update(extra)
    for entry_name, kwargs in files.items():
        (src / entry_name).write_text(_fabric_entry(**kwargs), encoding="utf-8")
    return src


def test_counts_per_kind_and_state(tmp_path):
    src = _write_corpus(tmp_path)
    vault = tmp_path / "vault"

    result = import_fabric(src, vault)

    # 12 files, 1 of them session noise -> 11 written, 1 skipped
    assert len(result.written) == 11
    assert len(result.skipped) == 1
    assert {(p.name, reason) for p, reason in result.skipped} == {
        ("11-session-low.md", "noise (training_value: low)")}

    assert result.by_kind == {
        "capture": 3, "conclusion": 2, "decision": 2, "fact": 1,
        "handoff": 1, "lesson": 1, "question": 1,
    }
    assert result.by_state == {"promoted": 9, "pruned": 1, "raw": 1}

    entries = {e.summary: e for _p, e in iter_entries(vault)}
    assert len(entries) == 11
    assert entries["Use json.loads for the frontmatter, not a regex"].kind == "decision"
    assert entries["Use json.loads for the frontmatter, not a regex"].state == "promoted"
    assert entries["A high-value session is real memory"].kind == "capture"
    assert entries["A high-value session is real memory"].state == "promoted"
    assert entries["An unknown type falls back to conclusion"].kind == "conclusion"
    # a low training value on a non-session entry is pruned, not skipped
    pruned = [e for e in entries.values() if e.state == "pruned"]
    assert len(pruned) == 1
    assert pruned[0].summary == "A pruned decision is a negative training example"
    # the reason is a tag, never a supersedes edge
    assert "pruned:low-training-value" in pruned[0].tags
    assert pruned[0].supersedes == []
    # a missing training value is raw, not promoted
    raw = [e for e in entries.values() if e.state == "raw"]
    assert len(raw) == 1
    assert raw[0].kind == "capture"
    # the duplicated H1/summary preamble is stripped from the body
    assert raw[0].stripped_body() == "the body of the entry"


def test_unicode_escape_and_colon_quotes_round_trip(tmp_path):
    src = _write_corpus(tmp_path, {
        "13-unicode.md": dict(fid="bbbb0001", type="decision", training_value="high",
                              summary="Two bugs — and the third one",
                              outcome="round-tripped", evidence="pytest"),
        "14-colon.md": dict(fid="bbbb0002", type="decision", training_value="high",
                            summary="Config: the \"quoted\" value stays quoted: really",
                            outcome="round-tripped", evidence="pytest"),
    })
    vault = tmp_path / "vault"
    import_fabric(src, vault)

    written = {e.summary: e for _p, e in iter_entries(vault)}
    # the \u2014 escape in the source frontmatter is an em dash in the entry
    assert "Two bugs — and the third one" in written
    unicode_entry = written["Two bugs — and the third one"]
    assert unicode_entry.summary == "Two bugs — and the third one"
    # a colon plus quotes survives the store's frontmatter round-trip
    entry = written["Config: the \"quoted\" value stays quoted: really"]
    assert entry.summary == "Config: the \"quoted\" value stays quoted: really"
    on_disk = entry_path(vault, entry.id, entry.summary).read_text(encoding="utf-8")
    assert json.loads(on_disk.split("summary: ", 1)[1].split("\n", 1)[0]) == entry.summary
    assert entry.evidence == "round-tripped — pytest"


def test_import_is_idempotent(tmp_path):
    src = _write_corpus(tmp_path)
    vault = tmp_path / "vault"

    first = import_fabric(src, vault)
    count_first = len(list(iter_entries(vault)))
    second = import_fabric(src, vault)
    count_second = len(list(iter_entries(vault)))

    assert count_first == count_second == 11
    assert [e.id for e in first.written] == [e.id for e in second.written]
    assert {e.id for e in first.written} == {e.id for e in second.written}


def test_dry_run_writes_nothing(tmp_path):
    src = _write_corpus(tmp_path)
    vault = tmp_path / "vault"

    result = import_fabric(src, vault, dry_run=True)

    assert len(result.written) == 11
    assert not vault.exists() or not list((vault / "notes").glob("*.md"))


def test_session_low_is_skipped_unless_include_noise(tmp_path):
    src = _write_corpus(tmp_path, {
        "15-code-session-low.md": dict(fid="bbbb0003", type="code-session",
                                       training_value="low",
                                       summary="Noise: fixed a typo in a comment",
                                       outcome="nothing worth remembering",
                                       evidence="git diff --stat"),
    })
    vault = tmp_path / "vault"

    result = import_fabric(src, vault)
    assert len(result.skipped) == 2
    assert len(result.written) == 11
    summaries = {e.summary for e in result.written}
    assert "Noise: fixed a typo in a comment" not in summaries
    assert "Noise: read a file and closed it" not in summaries

    noisy_vault = tmp_path / "noisy-vault"
    result = import_fabric(src, noisy_vault, include_noise=True)
    assert result.skipped == []
    assert len(result.written) == 13
    noise = {e.summary: e for e in result.written if e.summary.startswith("Noise:")}
    assert len(noise) == 2
    for entry in noise.values():
        assert entry.state == "pruned"
        assert entry.kind == "capture"


def test_template_shaped_frontmatter(tmp_path):
    """The corpus' own FRONTMATTER-TEMPLATE shape: trailing comments, inline tags."""
    src = tmp_path / "fabric"
    src.mkdir()
    (src / "entry.md").write_text(
        "---\n"
        'id: "e5a3"                      # unique 4-hex\n'
        'agent: "cipher"                 # YOUR profile name\n'
        'platform: "cli"\n'
        'timestamp: "2026-10-09T00:00:00Z"\n'
        'type: "code-session"            # MUST match the CONTENT\n'
        'tier: "hot"\n'
        'summary: "One line \\u2014 this is the retrieval signal"\n'
        'project_id: "shitter"           # real project\n'
        'status: "completed"\n'
        'outcome: "What actually happened"\n'
        'verified: "true"\n'
        'evidence: "The exact command run + the observed output"\n'
        'training_value: "high"          # high | normal | low\n'
        "tags: [shitter, refactor]\n"
        "---\n\n"
        "# Body\n\n"
        "What was asked, what you changed, what you could NOT do.\n",
        encoding="utf-8",
    )
    vault = tmp_path / "vault"

    result = import_fabric(src, vault)

    assert len(result.written) == 1
    entry = result.written[0]
    assert entry.id == "e5a3"
    assert entry.kind == "capture"
    assert entry.state == "promoted"
    assert entry.agent == "cipher"
    assert entry.project == "shitter"
    assert entry.summary == "One line — this is the retrieval signal"
    assert entry.evidence == "What actually happened — The exact command run + the observed output"
    assert entry.tags == ["shitter", "refactor"]
    assert entry.created == "2026-10-09T00:00:00Z"
    assert entry.source == "fabric:entry.md"
    # verified=true -> confidence 1.0; high training value -> importance 0.9
    assert entry.confidence == 1.0
    assert entry.importance == 0.9


def test_duplicate_and_missing_ids(tmp_path):
    """A duplicated fabric id must not collapse two entries into one."""
    src = tmp_path / "fabric"
    src.mkdir()
    (src / "a.md").write_text(_fabric_entry("dup1", type="decision", summary="first entry"),
                              encoding="utf-8")
    (src / "b.md").write_text(_fabric_entry("dup1", type="lesson", summary="second entry"),
                              encoding="utf-8")
    (src / "c.md").write_text(_fabric_entry(None, type="fact", summary="third entry"),
                              encoding="utf-8")
    vault = tmp_path / "vault"

    result = import_fabric(src, vault)

    assert len(result.written) == 3
    ids = [e.id for e in result.written]
    assert len(set(ids)) == 3
    # the id-less file gets a stable hash of its path
    by_summary = {e.summary: e for e in result.written}
    assert by_summary["third entry"].id == by_summary["third entry"].id
    assert len(by_summary["third entry"].id) == 8


def test_cli_import_fabric(tmp_path, capsys):
    src = _write_corpus(tmp_path)
    vault = tmp_path / "vault"

    code = main(["--vault", str(vault), "import", "fabric", str(src)])
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["written"] == 11
    assert report["skipped"] == 1
    assert report["by_kind"]["capture"] == 3
    assert report["by_state"]["promoted"] == 9
    assert len(list(iter_entries(vault))) == 11

    # --vault after the subcommand works too, and --dry-run writes nothing
    other = tmp_path / "other-vault"
    code = main(["import", "fabric", str(src), "--vault", str(other), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["written"] == 11
    assert not other.exists() or not list((other / "notes").glob("*.md"))

    # --vault between "import" and "fabric" works as well
    third = tmp_path / "third-vault"
    code = main(["import", "--vault", str(third), "fabric", str(src), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["vault"] == str(third)
    assert not third.exists() or not list((third / "notes").glob("*.md"))
