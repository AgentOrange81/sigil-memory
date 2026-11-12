"""`--session-db`: the reflection pass reads a live Hermes session from `state.db` (#101).

Hermes stopped writing `profiles/<p>/sessions/<id>.jsonl` for live sessions. Measured on this box:
43 files under `glitch/sessions/`, newest 2026-10-28, while `state.db` holds sessions through today
with none of them on disk. A hook pointed at the JSONL directory therefore reflects the past and
nothing else — and it exits 0 while doing it, which is the failure mode this project exists to stop.

The db holds everything, so the reader is selective on purpose: user and assistant prose by default
(18 KB against 376 KB of rows on one measured session), tool rows only when asked, capped and marked
where they were cut. An assistant row with no content is a tool-call turn, skipped and counted —
never rendered as an empty `assistant:`.

Fixture db is built here rather than copied: a state.db is large, user-owned, and changes shape, and
the five columns this reader uses are the whole contract.
"""

import json
import sqlite3
import subprocess
import sys

import pytest

from sigil import promote as promote_mod

SID = "local-0123456789abcdef0123456789abcdef"


def _db(tmp_path, rows, sid=SID):
    path = tmp_path / "state.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, last_activity_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                               tool_name TEXT, timestamp REAL);
    """)
    c.execute("INSERT INTO sessions VALUES (?, 1.0, 2.0)", (sid,))
    for i, (role, content, tool_name) in enumerate(rows, start=1):
        c.execute("INSERT INTO messages (id, session_id, role, content, tool_name, timestamp) "
                  "VALUES (?,?,?,?,?,?)", (i, sid, role, content, tool_name, 1000 + i))
    c.commit()
    c.close()
    return path


MIXED = [
    ("user", "why did the box lose its driver?", None),
    ("assistant", "", None),                       # a tool-call turn: no prose, must not render
    ("tool", "lsmod: no nvidia modules\n/lib/modules/7.0.0-43: no nvidia.ko", "terminal"),
    ("assistant", "The updater removed the three -615-open packages in the same\ntransaction.",
     None),
]


def test_prose_only_by_default(tmp_path):
    db = _db(tmp_path, MIXED)
    text = promote_mod.read_session_db(db, SID)
    assert text.splitlines()[0] == "user: why did the box lose its driver?"
    assert "assistant: The updater removed the three -615-open packages" in text
    assert "tool " not in text, "a tool row is not prose and is not included unasked"
    assert "assistant: \n" not in text + "\n", "an empty assistant turn must not render"
    assert promote_mod.LAST_LOAD_STATS.skipped == 2, "the tool row and the empty turn are counted"


def test_tools_on_request_are_capped_and_marked(tmp_path):
    db = _db(tmp_path, MIXED)
    text = promote_mod.read_session_db(db, SID, include_tools=True, tool_chars=20)
    line = next(ln for ln in text.splitlines() if ln.startswith("tool terminal:"))
    assert line.endswith("…[truncated]")
    assert len(line) < 60


def test_a_prefix_that_matches_two_sessions_is_refused(tmp_path):
    db = _db(tmp_path, MIXED)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO sessions VALUES (?, 1.0, 1.0)", ("local-0123456789abcdefFEDCBA",))
    c.commit()
    c.close()
    with pytest.raises(ValueError) as exc:
        promote_mod.read_session_db(db, "local-0123456789abcdef")
    assert "ambiguous" in str(exc.value)


def test_an_unknown_session_names_the_id(tmp_path):
    db = _db(tmp_path, MIXED)
    with pytest.raises(ValueError) as exc:
        promote_mod.read_session_db(db, "nope")
    assert "nope" in str(exc.value)


def test_a_session_with_no_prose_raises(tmp_path):
    db = _db(tmp_path, [("tool", "just output", "terminal")])
    with pytest.raises(ValueError) as exc:
        promote_mod.read_session_db(db, SID)
    assert "no user or assistant turns" in str(exc.value)


def test_the_rendered_transcript_chunks_on_turn_boundaries(tmp_path):
    """The join invariant that caught a chunker losing characters: chunks must reassemble exactly."""
    db = _db(tmp_path, [(role, content * 12, name) for role, content, name in MIXED])
    text = promote_mod.read_session_db(db, SID)
    chunks = promote_mod.chunk_transcript(text, 200)
    assert len(chunks) > 1, "fixture must be long enough to split"
    assert "\n".join(chunks) == text


def test_hook_reads_a_live_session_end_to_end(tmp_path):
    """`quipu hook --session-db` must reach the model with the prose, and write what it proposes."""
    import sigil.hook as hook_mod
    from sigil.config import load
    from sigil.store import iter_entries

    db = _db(tmp_path, MIXED)
    cfg = load(tmp_path / "vault", create=True)
    proposal = {"kind": "conclusion", "summary": "the driver loss came from the updater transaction",
                "evidence": "state.db session 0123456789: apt history", "importance": 0.9,
                "ask": "why did the box lose its nvidia driver?", "entities": [], "edges": [],
                "confidence": 0.8}
    provider = promote_mod.FakeProvider([proposal])
    transcript = promote_mod.read_session_db(db, SID)
    result = hook_mod.run(cfg, transcript, provider, agent="glitch", project="wraith",
                          session="0123456789abcdef", max_chars=0)
    assert len(result.written) == 1
    stored = [e for _p, e in iter_entries(cfg.root)]
    assert stored[0].session == "0123456789abcdef"
    assert stored[0].summary == "the driver loss came from the updater transaction"


def test_the_cli_accepts_the_flag(tmp_path):
    db = _db(tmp_path, MIXED)
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([{"kind": "conclusion", "summary": "a proposal from the db session",
                                 "evidence": "fixture", "importance": 0.9,
                                 "ask": "what did the db session propose?", "entities": [],
                                 "edges": [], "confidence": 0.8}]), encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)  # a vault has to exist: a wrong path is refused (#141)
    proc = subprocess.run([sys.executable, "-m", "sigil", "--vault", str(vault), "hook",
                           "--session-db", str(db), "--session", SID, "--fake", str(fake),
                           "--max-chars", "0", "--agent", "glitch"],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 entries, 1 promoted" in proc.stdout, proc.stdout


def test_include_tools_reaches_the_prompt(tmp_path):
    """The flag is documented, so it must do something observable: the prompt carries the tool line."""
    db = _db(tmp_path, MIXED)
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)  # a vault has to exist: a wrong path is refused (#141)
    proc = subprocess.run([sys.executable, "-m", "sigil", "--vault", str(vault), "hook",
                           "--session-db", str(db), "--session", SID, "--include-tools",
                           "--dry-run", "--max-chars", "0"],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "tool terminal: lsmod: no nvidia modules" in proc.stdout
    assert "why did the box lose its driver?" in proc.stdout
