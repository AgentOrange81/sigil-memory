"""`quipu hook` (#3) — the session-end adapter, and the failure paths that make it worth having.

A hook that exits 0 after writing nothing is the exact failure this project exists because of, so these
tests are mostly about the failure paths:

- a fake provider writes the entries and exits 0;
- a provider that raises exits 1, says why, and leaves the vault exactly as it was;
- a provider that never answers is cut off by --timeout instead of blocking session end;
- `QUIPU_HOOK_DRY_RUN=1` prints the prompt and writes nothing;
- a missing transcript is a failure, not an empty pass.
"""
import json
import time

import pytest

from sigil import hook as hook_mod
from sigil import index as index_mod
from sigil import promote as promote_mod
from sigil.config import load
from sigil.store import iter_entries

PROPOSAL = {
    "kind": "conclusion",
    "summary": "the hook wrote this entry at session end",
    "evidence": "pytest run of tests/test_hook.py",
    "importance": 0.9,
    "ask": "what does the session-end hook write?",
    "entities": [],
    "edges": [],
    "confidence": 0.8,
}


def _fake(tmp_path, proposals=None):
    path = tmp_path / "fake.json"
    path.write_text(json.dumps(proposals if proposals is not None else [PROPOSAL]), encoding="utf-8")
    return promote_mod.FakeProvider(json.loads(path.read_text(encoding="utf-8")))


def _transcript(tmp_path, text="user: we fixed the hook\nassistant: entries written\n"):
    path = tmp_path / "session.md"
    path.write_text(text, encoding="utf-8")
    return path


def _entries(cfg):
    return [e for _p, e in iter_entries(cfg.root)]


def test_a_successful_pass_writes_the_entry_and_indexes_it(tmp_path):
    cfg = load(tmp_path / "vault", create=True)
    result = hook_mod.run(cfg, _transcript(tmp_path), _fake(tmp_path), agent="glitch", project="quipu")

    assert len(result.written) == 1 and result.promoted == 1
    assert "1 entries, 1 promoted" in result.line()
    stored = _entries(cfg)
    assert [e.summary for e in stored] == ["the hook wrote this entry at session end"]
    assert stored[0].evidence, "a promoted entry without evidence is a bug"
    # indexed, not merely on disk: an MCP/framework caller cannot run reindex afterwards
    hits = index_mod.search(cfg.db_path, "session end hook")
    assert [h["id"] for h in hits] == [stored[0].id]


def test_a_provider_that_raises_exits_non_zero_and_writes_nothing(tmp_path):
    cfg = load(tmp_path / "vault", create=True)

    class Broken:
        def complete(self, prompt):
            raise ConnectionResetError("connection reset by peer")

    with pytest.raises(hook_mod.HookError) as caught:
        hook_mod.run(cfg, _transcript(tmp_path), Broken())
    assert "reflection failed" in str(caught.value)
    assert "ConnectionResetError" in str(caught.value)
    assert _entries(cfg) == [], "a failed pass must not leave a partial entry behind"


def test_a_hung_provider_is_cut_off_by_the_timeout(tmp_path):
    cfg = load(tmp_path / "vault", create=True)

    class Silent:
        def complete(self, prompt):
            time.sleep(30)
            return "[]"

    started = time.monotonic()
    with pytest.raises(hook_mod.HookError) as caught:
        hook_mod.run(cfg, _transcript(tmp_path), Silent(), timeout=0.2)
    assert "timed out after 0.2s" in str(caught.value)
    assert time.monotonic() - started < 5, "the timeout must return immediately, not after the call"
    assert _entries(cfg) == []


def test_dry_run_prints_the_prompt_and_writes_nothing(tmp_path):
    cfg = load(tmp_path / "vault", create=True)

    class Exploding:
        def complete(self, prompt):  # pragma: no cover - it must never be called
            raise AssertionError("dry run must not call the model")

    result = hook_mod.run(cfg, _transcript(tmp_path), Exploding(), dry_run=True)
    assert "transcript" in result.prompt.lower() or "session" in result.prompt.lower()
    assert result.written == []
    assert _entries(cfg) == []


def test_the_env_var_is_the_documented_switch():
    assert hook_mod.dry_run_requested({"QUIPU_HOOK_DRY_RUN": "1"})
    assert hook_mod.dry_run_requested({"QUIPU_HOOK_DRY_RUN": "true"})
    assert not hook_mod.dry_run_requested({"QUIPU_HOOK_DRY_RUN": "0"})
    assert not hook_mod.dry_run_requested({})


def test_a_missing_transcript_is_a_failure_not_an_empty_pass(tmp_path):
    cfg = load(tmp_path / "vault", create=True)
    with pytest.raises(hook_mod.HookError) as caught:
        hook_mod.run(cfg, tmp_path / "nope.md", _fake(tmp_path))
    assert "transcript not found" in str(caught.value)


def test_an_empty_transcript_is_a_failure(tmp_path):
    cfg = load(tmp_path / "vault", create=True)
    with pytest.raises(hook_mod.HookError) as caught:
        hook_mod.run(cfg, _transcript(tmp_path, "   \n\n"), _fake(tmp_path))
    assert "empty transcript" in str(caught.value)


def test_a_chunked_pass_writes_one_entry_per_claim_not_one_per_chunk(tmp_path):
    """Real data caught this: at --max-chars 1500 a 14 KB transcript became 8 chunks and the same fake
    proposal was written 8 times. Chunking must not turn one claim into a pile of near-duplicates."""
    cfg = load(tmp_path / "vault", create=True)
    turns = "\n".join(f"user: turn {i} " + "x" * 180 for i in range(12))
    transcript = tmp_path / "long.md"
    transcript.write_text(turns, encoding="utf-8")

    result = hook_mod.run(cfg, transcript, _fake(tmp_path), max_chars=400)

    assert result.chunks > 1, "the fixture must actually chunk, or this test proves nothing"
    assert result.proposed == 1, f"the same summary came back {result.proposed} times"
    assert len(_entries(cfg)) == 1, "one claim must be one entry, however many chunks saw it"


# --- the exit codes are the contract: a framework reads nothing else -------------------


def test_the_cli_exits_zero_on_a_completed_pass(cfg, capsys, tmp_path):
    from sigil.cli import main

    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([PROPOSAL]), encoding="utf-8")
    code = main(["--vault", str(cfg.root), "hook", "--transcript", str(_transcript(tmp_path)),
                 "--fake", str(fake), "--agent", "glitch"])
    assert code == 0
    assert "1 entries, 1 promoted" in capsys.readouterr().out


def test_the_cli_exits_one_with_a_reason_when_the_pass_fails(cfg, capsys, tmp_path):
    from sigil.cli import main

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    code = main(["--vault", str(cfg.root), "hook", "--transcript", str(_transcript(tmp_path)),
                 "--fake", str(broken)])
    assert code == 1
    err = capsys.readouterr().err
    assert "hook failed" in err
    assert _entries(cfg) == [], "a failed pass must not write"


def test_the_cli_honours_the_dry_run_env_var(cfg, capsys, monkeypatch, tmp_path):
    from sigil.cli import main

    monkeypatch.setenv("QUIPU_HOOK_DRY_RUN", "1")
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([PROPOSAL]), encoding="utf-8")
    code = main(["--vault", str(cfg.root), "hook", "--transcript", str(_transcript(tmp_path)),
                 "--fake", str(fake)])
    assert code == 0
    out = capsys.readouterr().out
    assert "summary" in out.lower(), "dry run prints the prompt, which carries the vault's summaries"
    assert _entries(cfg) == []



# --- #111: a chunk failure must not throw away the chunks that already landed -------------------

class _FlakyProvider:
    """Answers the first `ok_chunks` calls, then raises. Counts calls so a retry can be inspected."""

    def __init__(self, ok_chunks: int, summaries: list[str]):
        self.ok_chunks = ok_chunks
        self.summaries = summaries
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.calls > self.ok_chunks:
            raise ValueError("Expecting ':' delimiter: line 9 column 3 (the model mangled the JSON)")
        summary = self.summaries[(self.calls - 1) % len(self.summaries)]
        return json.dumps({"entries": [{
            "kind": "fact", "summary": summary, "evidence": "fixture", "importance": 0.9,
            "ask": "what happened?", "entities": [], "edges": [], "confidence": 0.8}]})


def _chunked_transcript(tmp_path, chunks: int = 3):
    turns = "\n".join(f"user: turn {i} " + "y" * 200 for i in range(chunks * 2))
    path = tmp_path / "chunked.md"
    path.write_text(turns, encoding="utf-8")
    return path


def test_a_failure_on_a_later_chunk_keeps_what_landed(tmp_path):
    cfg = load(tmp_path / "vault", create=True)
    provider = _FlakyProvider(ok_chunks=1, summaries=["claim from the first chunk", "two", "three"])

    with pytest.raises(hook_mod.HookError) as caught:
        hook_mod.run(cfg, _chunked_transcript(tmp_path), provider, session="sess-111", max_chars=400)

    assert "chunk 2 of" in str(caught.value), str(caught.value)
    assert "1 chunk(s) applied" in str(caught.value)
    written = [e.summary for e in _entries(cfg)]
    assert written == ["claim from the first chunk"], f"the landed chunk must survive, got {written}"


def test_a_retry_reads_only_the_chunks_that_did_not_land(tmp_path):
    cfg = load(tmp_path / "vault", create=True)
    transcript = _chunked_transcript(tmp_path)
    first = _FlakyProvider(ok_chunks=1, summaries=["claim from the first chunk", "two", "three"])
    with pytest.raises(hook_mod.HookError):
        hook_mod.run(cfg, transcript, first, session="sess-111", max_chars=400)
    assert first.calls == 2, "the first attempt reads chunk 1 and fails on chunk 2"

    total = len(promote_mod.chunk_transcript(
        hook_mod.read_transcript(transcript), 400))
    second = _FlakyProvider(ok_chunks=99, summaries=["claim from the first chunk", "two", "three"])
    result = hook_mod.run(cfg, transcript, second, session="sess-111", max_chars=400)

    assert second.calls == total - 1, (
        f"the retry must skip the {total}-chunk pass's first chunk, made {second.calls} calls for "
        f"{total} chunks")
    assert result.resumed == 1, f"one chunk was resumed, got {result.resumed}"
    # the fake reuses three summaries across `total` chunks, so the count proves two things at once:
    # every remaining chunk was read, and a claim the resumed chunk already wrote is not written again
    assert sum(1 for e in _entries(cfg) if e.summary == "claim from the first chunk") == 1
    assert len({e.summary for e in _entries(cfg)}) == len(_entries(cfg)), "no claim twice"
    assert sum(1 for e in _entries(cfg) if e.summary == "claim from the first chunk") == 1
    assert "1 resumed" in result.line()


def test_the_hook_records_the_model_that_answered(tmp_path):
    """#110: `[llm] model` is the literal string 'local', i.e. whatever :8080 serves. An entry that
    does not name its model cannot say which weights produced it once the quant changes."""
    cfg = load(tmp_path / "vault", create=True)

    class Named:
        model = "qwen3.6-35b-a3b"

        def complete(self, prompt: str) -> str:
            return json.dumps({"entries": [dict(PROPOSAL)]})

    hook_mod.run(cfg, _transcript(tmp_path), Named(), agent="glitch", session="s-model")
    (entry,) = _entries(cfg)
    assert entry.model == "qwen3.6-35b-a3b", entry.model


def test_a_configured_name_of_local_is_replaced_by_what_the_endpoint_serves(tmp_path, monkeypatch):
    """#110 follow-up, measured: the vault's `[llm] model` is the literal string "local", so recording
    `provider.model` would have written "local" on every entry — the placeholder the issue was about."""
    cfg = load(tmp_path / "vault", create=True)
    cfg.llm_model = "local"

    class Answers:
        model = "local"

        def complete(self, prompt: str) -> str:
            return json.dumps({"entries": [dict(PROPOSAL)]})

    monkeypatch.setattr(hook_mod, "served_model", lambda url, timeout=3.0: "qwen3.6-35b-a3b")
    hook_mod.run(cfg, _transcript(tmp_path), Answers(), session="s-local")
    (entry,) = _entries(cfg)
    assert entry.model == "qwen3.6-35b-a3b", entry.model


def test_a_naming_lookup_that_fails_does_not_fail_the_pass(tmp_path, monkeypatch):
    cfg = load(tmp_path / "vault", create=True)

    class Answers:
        model = "local"

        def complete(self, prompt: str) -> str:
            return json.dumps({"entries": [dict(PROPOSAL)]})

    monkeypatch.setattr(hook_mod, "served_model", lambda url, timeout=3.0: "")
    result = hook_mod.run(cfg, _transcript(tmp_path), Answers(), session="s-fallback")
    assert result.promoted == 1
    assert _entries(cfg)[0].model in ("", "local")


def test_the_index_carries_what_the_markdown_records(tmp_path):
    """#126 follow-up: the pass ends with a rebuild, so the derived columns must be filled then.

    Measured on the live vault while checking the backfill: a read of `.sigil/index.db` straight after
    a pass returned NULL `evidence_kind` and `model` on every entry the pass had written, while the
    markdown carried both. The columns were correct again after an explicit `reindex`, so the reader
    had raced the rebuild — but a reader cannot tell those apart, and the invariant at stake is the
    project's whole premise: delete the index and rebuild it, and nothing is lost.
    """
    import sqlite3

    cfg = load(tmp_path / "vault", create=True)

    class Named:
        model = "qwen3.6-35b-a3b"

        def complete(self, prompt: str) -> str:
            return json.dumps({"entries": [dict(PROPOSAL)]})

    hook_mod.run(cfg, _transcript(tmp_path), Named(), agent="glitch", session="s-index")
    (entry,) = _entries(cfg)
    assert entry.evidence_kind, "the entry itself must know what its evidence is"

    c = sqlite3.connect(cfg.db_path)
    try:
        row = c.execute("SELECT model, evidence_kind FROM entries WHERE id = ?", (entry.id,)).fetchone()
    finally:
        c.close()
    assert row == ("qwen3.6-35b-a3b", entry.evidence_kind), row
