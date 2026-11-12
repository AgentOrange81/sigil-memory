import json

from sigil.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_end_to_end(cfg, capsys, tmp_path):
    v = str(cfg.root)
    assert run(capsys, "--vault", v, "init")[0] == 0
    assert run(capsys, "--vault", v, "write", "--kind", "decision", "--state", "promoted",
               "--summary", "Markdown is the source of truth", "--ask", "where does quipu store data?",
               "--evidence", "40 ms reindex over 822 notes", "--project", "quipu",
               "--agent", "glitch", "--importance", "0.9")[0] == 0
    code, out = run(capsys, "--vault", v, "search", "markdown")
    assert code == 0 and "source of truth" in out
    code, out = run(capsys, "--vault", v, "edge", "quipu", "depends_on", "sqlite")
    assert code == 0 and out.strip().isdigit()
    code, out = run(capsys, "--vault", v, "timeline", "quipu")
    assert code == 0 and "depends_on" in out
    code, out = run(capsys, "--vault", v, "stats")
    report = json.loads(out)
    assert report["promoted"] == 1 and report["sft_examples"] == 1
    out_path = tmp_path / "sft.jsonl"
    assert run(capsys, "--vault", v, "export", "sft", "--out", str(out_path))[0] == 0
    assert out_path.exists() and out_path.read_text().strip()


def test_audit_flags_promoted_without_evidence(cfg, capsys):
    v = str(cfg.root)
    run(capsys, "--vault", v, "write", "--kind", "fact", "--state", "promoted",
        "--summary", "unproven claim", "--importance", "0.9")
    code, out = run(capsys, "--vault", v, "audit")
    assert code == 1 and "promoted without evidence" in out


def test_capture_then_reflect_with_fake_provider(cfg, capsys, tmp_path):
    v = str(cfg.root)
    fake = tmp_path / "resp.json"
    fake.write_text(json.dumps([{
        "kind": "lesson", "summary": "rsync over a live sqlite db corrupts it",
        "evidence": "integrity_check failed on the target", "importance": 0.95,
        "ask": "how do I move a hermes profile?",
    }]))
    transcript = tmp_path / "session.md"
    transcript.write_text(
        "user: how do I move a hermes profile?\n"
        "assistant: rsync corrupts it, use sqlite3 .backup\n"
    )
    code, out = run(capsys, "--vault", v, "reflect", str(transcript),
                    "--fake", str(fake), "--agent", "glitch")
    assert code == 0 and "1 promoted" in out
    assert run(capsys, "--vault", v, "stats")[0] == 0
