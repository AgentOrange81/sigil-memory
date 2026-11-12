"""Acceptance gate for the transcript loader and chunker (cipher's transcript-chunking branch).

Black-box contract tests, written against the CONTRACT — `sigil.promote.load_transcript`,
`sigil.promote.chunk_transcript`, and `quipu reflect --max-chars N` — never against an
implementation. On main (a6f305c) none of it exists yet: these tests fail there on purpose, and
they are the gate cipher's PR must satisfy.

Contract, from the task: `load_transcript(path_or_text)` renders Hermes session JSONL (role/content,
where content is a string or a list of text parts) as `role: text` lines, passes plain markdown
through, and skips malformed lines without dying. `chunk_transcript(text, max_chars=24000)` splits
on turn boundaries only, keeps a single oversized turn whole, and treats max_chars <= 0 as no
chunking. `quipu reflect` against a dead endpoint must fail loudly: non-zero exit and an error that
names the endpoint URL.

Deterministic: tmp files only, no network access to real services, ports that refuse instantly, no
sleeps anywhere.
"""

import json

import pytest

from sigil import config as config_mod
from sigil import promote
from sigil.cli import main as cli_main

DEAD_ENDPOINT = "http://127.0.0.1:9/v1"  # port 9 refuses instantly; nothing can hang on it


def _vault_with_dead_endpoint(tmp_path):
    vault = tmp_path / "vault"
    cfg = config_mod.load(vault, create=True)
    cfg.llm_base_url = DEAD_ENDPOINT
    cfg.save()
    return vault


def test_jsonl_shapes_string_content_parts_meta_and_malformed(tmp_path):
    """Session JSONL: string content, list-of-text-parts content, a non-message row and one
    malformed line. The two messages come back as `role: text` lines, in order, and the loader
    keeps serving after the junk."""
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([
        '{"role": "user", "content": "did the BIOS flash fix the reboots?"}',
        json.dumps({"role": "assistant",
                    "content": [{"type": "text", "text": "yes, three boots, zero MCE lines"}]}),
        json.dumps({"type": "meta", "id": "meta-row-marker-99", "session": "x"}),
        "{not json",
        json.dumps({"role": "user",
                    "content": [{"type": "text", "text": "and the 649 CCEs? "},
                                {"type": "text", "text": "still there?"}]}),
    ]) + "\n", encoding="utf-8")

    out = promote.load_transcript(path)

    assert "user: did the BIOS flash fix the reboots?" in out
    assert "assistant: yes, three boots, zero MCE lines" in out
    assert "user: and the 649 CCEs? still there?" in out, (
        "the parts of one message must all be rendered, in the role: text line for that message")
    assert out.index("did the BIOS flash") < out.index("yes, three boots"), (
        "transcript order must be preserved")
    assert "meta-row-marker-99" not in out, "a non-message row must not enter the transcript"
    assert "{not json" not in out, "a malformed line must be skipped, not fatal"


def test_markdown_passthrough_keeps_the_blocks_in_order(tmp_path):
    """Plain markdown passes through essentially intact — by path and by raw text."""
    first = "user: did the flash fix the reboots?"
    second = "assistant: yes, three consecutive boots with zero MCE lines after 2601"
    path = tmp_path / "notes.md"
    path.write_text(f"# session log\n\n{first}\n\n{second}\n\ntail note\n", encoding="utf-8")

    from_path = promote.load_transcript(path)
    assert first in from_path and second in from_path
    assert from_path.index(first) < from_path.index(second), "order must be preserved"
    from_text = promote.load_transcript(path.read_text(encoding="utf-8"))
    assert from_text == from_path, (
        "markdown passed as text and markdown passed as a path are the same contract")


def test_chunking_splits_on_turn_boundaries_only():
    """Three 120-char turns, cap ~1.5 turns: chunks start at turn boundaries, no turn is split,
    and the chunks carry the transcript in order."""

    def turn(i: int, role: str) -> tuple[str, str]:
        marker = f"<TURN{i}>"
        base = f"{role}: {marker} turn {i} "
        text = base + "y" * (120 - len(base))
        assert len(text) == 120
        return text, marker

    turns, markers = zip(*(turn(i, "user" if i % 2 else "assistant") for i in (1, 2, 3)), strict=True)
    text = "\n".join(turns)

    chunks = promote.chunk_transcript(text, max_chars=180)
    assert chunks, "chunking must return at least one chunk"
    for chunk in chunks:
        assert chunk.startswith(("user: ", "assistant: ")), \
            f"every chunk must begin at a turn boundary, got: {chunk[:40]!r}"
        for marker in markers:
            assert chunk.count(marker) <= 1, \
                f"a turn was split across two chunks: {chunk[:40]!r}"
    for marker in markers:
        occurrences = sum(chunk.count(marker) for chunk in chunks)
        assert occurrences == 1, f"{marker} appears {occurrences}x — a turn was split or lost"
    assert "\n".join(chunks) == text, \
        "concatenating the chunks must reproduce all three turns in order"


def test_an_oversized_turn_is_kept_whole():
    """A single 500-char turn with max_chars=100 comes back untruncated and unsplit."""
    turn = "user: " + "z" * 494
    assert len(turn) == 500
    chunks = promote.chunk_transcript(turn, max_chars=100)
    assert len(chunks) == 1, f"an oversized turn is its own chunk, not several: {len(chunks)}"
    assert chunks[0] == turn, "the oversized turn must be kept whole, not truncated"


def test_max_chars_at_or_below_zero_disables_chunking():
    text = ("user: first turn of a longer session\n"
            "assistant: second turn of a longer session\n") * 30
    for limit in (0, -1):
        assert promote.chunk_transcript(text, max_chars=limit) == [text], \
            f"max_chars={limit} must disable chunking"


def test_reflect_without_a_reachable_endpoint_fails_loud(tmp_path, capsys):
    """`quipu reflect` against a dead endpoint exits non-zero and names the endpoint URL."""
    vault = _vault_with_dead_endpoint(tmp_path)
    tx = tmp_path / "tiny.md"
    tx.write_text("user: why did the box reboot?\nassistant: three boots, zero MCE lines\n",
                  encoding="utf-8")

    try:
        rc = cli_main(["--vault", str(vault), "reflect", str(tx)])
    except Exception as exc:  # noqa: BLE001 - the pre-implementation behaviour: the CLI leaks
        pytest.fail(f"the CLI leaked {type(exc).__name__} instead of failing loudly: {exc}")
    assert rc != 0, "a reflect pass that reached nothing must exit non-zero"
    captured = capsys.readouterr()
    assert "127.0.0.1:9" in captured.err + captured.out, (
        "the error message must name the endpoint URL, or nobody can tell which endpoint died")