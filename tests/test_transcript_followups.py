"""Regression tests for the five defects found in #2 after it merged (c476a61).

Each test names the defect it pins, so a change that reopens one says which one it reopened.
Deterministic: tmp vaults, no network, no sleeps.
"""

import json

import pytest

from sigil import config as config_mod
from sigil import hook as hook_mod
from sigil import promote as promote_mod
from sigil.cli import main as cli_main

FAKE = [{"kind": "fact", "summary": "one claim", "evidence": "a log line",
         "importance": 0.9, "ask": "what did the log say?"}]


def _vault(tmp_path):
    return config_mod.load(tmp_path / "vault", create=True)


def _fake_file(tmp_path):
    path = tmp_path / "fake.json"
    path.write_text(json.dumps(FAKE), encoding="utf-8")
    return path


def _long_transcript(tmp_path):
    """Six turns, long enough that `--max-chars 400` splits it into more than one chunk."""
    path = tmp_path / "long.md"
    path.write_text("".join(f"user: turn {i} " + "x" * 120 + "\n" for i in range(6)), encoding="utf-8")
    return path


def test_a_dry_run_prints_exactly_one_json_document(tmp_path, capsys):
    """Defect: the chunk breakdown went to stdout ahead of the json, so stdout was not a document."""
    vault = _vault(tmp_path)
    rc = cli_main(["--vault", str(vault.root), "reflect", str(_long_transcript(tmp_path)),
                   "--fake", str(_fake_file(tmp_path)), "--max-chars", "400", "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    payload = json.loads(captured.out)
    assert isinstance(payload, list) and payload, "stdout must be one json document"
    assert "chunk " not in captured.out, f"the breakdown polluted the json: {captured.out!r}"
    assert "chunk " in captured.err, "the breakdown belongs on stderr"


def test_a_normal_run_keeps_its_breakdown_on_stdout(tmp_path, capsys):
    """Only the dry-run contract is at stake: a normal run still reports where it is."""
    vault = _vault(tmp_path)
    rc = cli_main(["--vault", str(vault.root), "reflect", str(_long_transcript(tmp_path)),
                   "--fake", str(_fake_file(tmp_path)), "--max-chars", "400"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "chunk 1/" in captured.out, captured.out
    assert captured.out.strip().endswith("left raw"), captured.out


def test_the_loader_counts_the_lines_it_skipped(tmp_path):
    """Defect: a malformed line was skipped silently — a session that loses lines without saying so."""
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "hello"}) + "\n"
        + "{not json\n"
        + json.dumps({"role": "session_meta", "content": "meta"}) + "\n"
        + json.dumps({"role": "assistant", "content": "reply"}) + "\n",
        encoding="utf-8")

    out = promote_mod.load_transcript(path)
    assert out.splitlines() == ["user: hello", "assistant: reply"]
    assert promote_mod.LAST_LOAD_STATS.malformed == 1, "the corrupt line must be counted"
    assert promote_mod.LAST_LOAD_STATS.skipped == 1, "the meta row must be counted"
    assert promote_mod.LAST_LOAD_STATS.messages == 2

    own = promote_mod.LoadStats()
    promote_mod.load_transcript(path, own)
    assert own.malformed == 1 and own.skipped == 1 and own.messages == 2, \
        "a caller can take the counts instead of reading the module global"


def test_the_cli_reports_malformed_lines(tmp_path, capsys):
    """The count has to reach the operator, not just the module."""
    vault = _vault(tmp_path)
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"role": "user", "content": "hello"}) + "\n{not json\n",
                    encoding="utf-8")
    rc = cli_main(["--vault", str(vault.root), "reflect", str(path), "--fake",
                   str(_fake_file(tmp_path))])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "1 malformed lines skipped" in captured.err, captured.err


def test_the_hook_reads_a_session_through_the_loader(tmp_path):
    """Defect: the hook fed the model raw json lines instead of the rendered turns."""
    cfg = _vault(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"role": "user", "content": "fix the hook"}) + "\n",
                       encoding="utf-8")
    result = hook_mod.run(cfg, session, promote_mod.FakeProvider([]), dry_run=True)
    assert "user: fix the hook" in result.prompt, result.prompt
    assert '"role"' not in result.prompt, "the raw json must not reach the prompt"


def test_the_hook_speaks_hook_error_for_a_missing_transcript(tmp_path):
    """The loader raises ValueError; the hook still speaks one exception type."""
    cfg = _vault(tmp_path)
    with pytest.raises(hook_mod.HookError) as excinfo:
        hook_mod.run(cfg, tmp_path / "nope.md", promote_mod.FakeProvider([]))
    assert "transcript not found" in str(excinfo.value), str(excinfo.value)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("payload", [b"<html>not json</html>", b"{}", b'{"choices": []}'],
                         ids=["not-json", "no-choices", "empty-choices"])
def test_a_bad_response_names_the_endpoint_url(monkeypatch, payload):
    """Defect: a non-json body or a choice-less payload leaked JSONDecodeError/KeyError, and the
    failure did not name the url that was actually posted to."""
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr(promote_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as excinfo:
        promote_mod.HttpProvider("http://127.0.0.1:9/v1", "m", "").complete("hi")
    assert "http://127.0.0.1:9/v1/chat/completions" in str(excinfo.value), str(excinfo.value)


def test_a_transport_failure_names_the_endpoint_url(monkeypatch):
    """The same rule for a dead socket: the url is in the message."""
    def fake_urlopen(req, timeout=None):
        raise OSError("Connection refused")

    monkeypatch.setattr(promote_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as excinfo:
        promote_mod.HttpProvider("http://127.0.0.1:9/v1", "m", "").complete("hi")
    assert "http://127.0.0.1:9/v1/chat/completions" in str(excinfo.value), str(excinfo.value)


def test_a_single_turn_transcript_is_never_split():
    """Defect: the chunker needed two turn lines before it would treat the text as turns, so a
    one-turn transcript fell back to blank-line splitting and could be cut inside the message."""
    text = "user: one message\n\nstill the same message\n"
    assert promote_mod.chunk_transcript(text, max_chars=10) == [text], \
        "one turn is one chunk, whatever the cap"
