"""#109 — the promotion gate asks whether evidence exists; `evidence_kind` records what it IS.

The gate stays as it was, because a 9-byte "see log" is a weak memory, not a false one: refusing it
would cost the claim and still leave the corpus unable to say how much of it is re-checkable. The
classification is deterministic — no model call, no drift — and the property is recorded, indexed and
filterable, so "present" and "re-checkable" stop being the same word.
"""
import json

import pytest

from sigil import cli
from sigil import evidence as evidence_mod
from sigil import export as export_mod
from sigil import index as index_mod
from sigil import promote as promote_mod
from sigil.config import load
from sigil.store import Entry, iter_entries, write_entry


@pytest.mark.parametrize("text, kind", [
    # commands: something a person could re-run
    ("`quipu reindex` dropped it", "command"),
    ("$ systemctl --user restart quipu-capture", "command"),
    ("ps -eo comm,args | grep nuq-worker", "command"),
    ("pytest tests/test_hook.py", "command"),
    ("tail ~/admin/logs/quipu-capture.log", "command"),
    ("sqlite3 index.db 'select count(*) from entries'", "command"),
    # paths: something a person could open
    ("docs/USING.md:316-318", "path"),
    ("quipu/hook.py:76", "path"),
    ("MEDIA:/home/dirk/reports/strata-3card-2026-11-05.html", "path"),
    ("`docs/moe-cache.md` lives on a PR branch", "path"),
    ("moe-bench.py did not exist on wraith", "path"),
    # urls
    ("see https://example.com/runs/42715230290", "url"),
    # claims: prose that points at nothing re-checkable
    ("see log", "claim"),
    ("9 bytes", "claim"),
    ("378 passed, 1 xfailed", "claim"),
    ("acceptance: 0.697 (XXS) vs 0.871 (IQ3_S)", "claim"),
    ("", "claim"),
])
def test_the_evidence_kind_is_classified_from_the_text(text, kind):
    assert evidence_mod.classify(text) == kind


def test_a_proposal_that_cites_a_command_is_promoted_and_labelled(cfg):
    proposal = {"kind": "lesson", "summary": "reindex drops an edge whose carrier is gone",
                "evidence": "`quipu reindex` printed 'skipped 3 edge specs'",
                "importance": 0.9, "ask": "what happens to an edge with no carrier?",
                "entities": [], "edges": [], "confidence": 0.8}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg)
    assert written[0].evidence_kind == "command"

    # markdown is the truth: the kind survives a round trip and a rebuild
    path = next(cfg.root.glob(f"notes/{written[0].id}*.md"))
    assert "evidence_kind: command" in path.read_text(encoding="utf-8")
    assert Entry.from_markdown(path.read_text(encoding="utf-8")).evidence_kind == "command"


def test_thin_evidence_still_promotes_and_says_so(cfg):
    """The decision that closed #109: a weak memory is a memory. The gate does not change."""
    proposal = {"kind": "fact", "summary": "it worked", "evidence": "see log", "importance": 0.9,
                "ask": "did the fix actually make it work?", "entities": [], "edges": [], "confidence": 0.9}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg)
    assert written[0].state == "promoted"
    assert written[0].evidence_kind == "claim"


def test_the_sft_export_can_filter_on_evidence_without_touching_the_vault(cfg):
    good = {"kind": "fact", "summary": "the box has three cards", "evidence": "`nvidia-smi` shows 3",
            "importance": 0.9, "ask": "how many GPUs does it have?", "entities": [], "edges": [],
            "confidence": 0.9}
    thin = {"kind": "fact", "summary": "it works now", "evidence": "see log", "importance": 0.9,
            "ask": "did the change actually take effect?", "entities": [], "edges": [], "confidence": 0.9}
    promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [good, thin]})), cfg)

    out = cfg.root.parent / "sft-all.jsonl"
    assert export_mod.export_sft(cfg, out) == 2
    filtered = cfg.root.parent / "sft-filtered.jsonl"
    assert export_mod.export_sft(cfg, filtered, evidence_kinds={"command", "path", "url"}) == 1

    # and the vault still holds both: filtering is a view, not a delete
    assert len([e for _p, e in iter_entries(cfg.root)]) == 2


def test_stats_reports_the_evidence_mix(cfg):
    entries = [
        {"kind": "fact", "summary": "a", "evidence": "`quipu stats`", "importance": 0.9,
         "ask": "what do the stats command report?", "entities": [], "edges": [], "confidence": 0.9},
        {"kind": "fact", "summary": "b", "evidence": "see log", "importance": 0.9,
         "ask": "what does the evidence in the log say?", "entities": [], "edges": [], "confidence": 0.9},
    ]
    promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": entries})), cfg)
    report = export_mod.dataset_report(cfg)
    assert report["evidence_by_kind"] == {"command": 1, "claim": 1}
    assert report["evidence_bytes"]["min"] > 0


def test_the_reflection_prompt_asks_for_a_pointer_not_a_restatement():
    """The rule that moved the measured mix, pinned so a prompt edit cannot quietly undo it (#109).

    Measured on one fixed 89 KB transcript, same model and chunking, before and after: claim 63% → 50%,
    median evidence 74 B → 110 B, entries 35 → 36, and the pass took 629 s → 318 s.
    """
    from sigil.promote import PROMPT_PATH

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    flat = " ".join(prompt.split())  # the rules are prose: line wrapping is not part of them
    assert "RE-RUN or OPEN" in flat
    assert "A bare number is NOT evidence" in flat
    assert "no re-checkable evidence in the session" in flat
    # and the JSON example must not invite a number back in
    assert "number / command / path / log line" not in flat


def test_the_audit_sees_an_index_that_lost_what_the_markdown_kept(tmp_path):
    """#128: a writer running pre-#109 code rebuilt the live index and blanked `evidence_kind` and
    `model` on all 2064 rows, while the markdown still carried both. Nothing noticed: the audit read
    the markdown, the exports read the markdown, and only a raw SQL read of the index could tell.

    The audit is the surface that stands on the markdown, so it is the one that can say the derived
    copy no longer matches it.
    """
    import sqlite3

    cfg = load(tmp_path / "vault", create=True)
    for i, ev in enumerate(["ran `pytest -q`, 546 passed", "see /etc/fstab line 3"]):
        write_entry(cfg.root, Entry(id=f"beef000{i}", state="promoted", summary=f"finding {i}",
                                    evidence=ev, evidence_kind=evidence_mod.classify(ev)))
    index_mod.reindex(cfg.root, cfg.db_path)

    def audit_problems():
        import contextlib
        import io
        import json
        args = type("A", (), {"vault": str(cfg.root), "json": True})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_audit(args)
        return json.loads(buf.getvalue())["problems"]

    assert not [p for p in audit_problems() if "index lost" in p], "a clean rebuild must read clean"

    # this is exactly what the stale client did: rebuild with the pre-#109 column list
    c = sqlite3.connect(cfg.db_path)
    c.execute("UPDATE entries SET evidence_kind = NULL")
    c.commit()
    c.close()

    (problem,) = [p for p in audit_problems() if "index lost" in p]
    assert "2 entries" in problem, problem


def test_the_cli_writer_records_the_class_too(tmp_path, monkeypatch):
    """#128 fixed the MCP writer; the CLI writer kept producing unclassified notes for the same reason —
    omitting a derived field is not an error, so nothing complains and only a backfill heals it.

    The field must equal what `classify` says, not what the evidence feels like: the vocabulary is
    command / path / url / claim, and there is no "measurement" class to expect (#109).
    """
    vault = tmp_path / "v"
    monkeypatch.setattr("sys.argv", ["sigil", "--vault", str(vault), "init"])
    cli.main()
    evidence = "see `sigil --vault ~/sigil-vault audit` for the counts"
    monkeypatch.setattr("sys.argv", ["sigil", "--vault", str(vault), "write",
                                     "--summary", "the vault audit is the surface that proves a repair",
                                     "--evidence", evidence,
                                     "--agent", "glitch"])
    cli.main()

    cfg = load(vault)
    note = next(iter((cfg.root / "notes").glob("*.md")))
    front = note.read_text(encoding="utf-8").split("---")[1]

    assert f"evidence_kind: {evidence_mod.classify(evidence)}" in front, front
    assert "evidence_kind: command" in front, front
