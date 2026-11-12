"""The capture runner decides what the vault is made of, so its rules are tested like a write path.

Every case here was a measured failure before it was a rule: cron runs owning the head of the queue,
a captured session never being read again, three 2 KB sessions starving an 818 KB one, a re-pass that
was assumed to be tail-only, and an index a stale client had blanked.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from sigil import capture, evidence
from sigil import index as index_mod
from sigil.config import load
from sigil.store import Entry, write_entry


def make_profiles(tmp_path, sessions):
    """A profile tree shaped like Hermes': `sessions` is {session_id: prose_bytes}."""
    root = tmp_path / "profiles"
    for profile in ("alpha", "beta"):
        profile_dir = root / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "state.db").unlink(missing_ok=True)
        c = sqlite3.connect(profile_dir / "state.db")
        c.execute("CREATE TABLE sessions (id TEXT, last_activity_at REAL, started_at REAL, title TEXT)")
        c.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
        # one row per session, in one profile: a session lives in exactly one profile's state.db, and
        # writing each into both made every case look doubled
        for sid, prose in (sessions.items() if profile == "alpha" else []):
            stamp = time.time() - 3600  # idle an hour: finished, inside the window
            c.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, stamp, stamp, f"about {sid}"))
            c.execute("INSERT INTO messages VALUES (?,?,?)", (sid, "user", "x" * (prose // 2)))
            c.execute("INSERT INTO messages VALUES (?,?,?)", (sid, "assistant", "y" * (prose // 2)))
        c.commit()
        c.close()
    return root


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    """Point the whole runner at a throwaway profile tree and vault."""
    root = tmp_path / "vault"
    cfg = load(root, create=True)
    monkeypatch.setattr(capture, "VAULT", cfg.root)
    monkeypatch.setattr(capture, "PROFILES", make_profiles(tmp_path, {}))
    monkeypatch.setattr(capture, "DONE", tmp_path / "done.txt")
    monkeypatch.setattr(capture, "REPASS", tmp_path / "repass.json")
    monkeypatch.setattr(capture, "LOG", tmp_path / "capture.log")
    return cfg


def seed(cfg, entry_id, session="", evidence_text="ran `pytest -q`"):
    write_entry(cfg.root, Entry(id=entry_id, state="promoted", summary=f"claim {entry_id}",
                                evidence=evidence_text,
                                evidence_kind=evidence.classify(evidence_text), session=session))


def test_a_session_captured_before_the_ledger_recorded_prose_stays_closed(runner):
    """#126: the rule that re-admits a grown session cannot fire for a line with no size.

    Every session captured before the field existed would otherwise be re-read at once, which is a
    stampede over a fortnight of transcripts on the first tick after an upgrade.
    """
    capture.PROFILES = make_profiles(runner.root.parent, {"legacy": 3000})
    capture.DONE.write_text("legacy 2026-11-09T02:00:00 alpha entries=3\n")

    assert capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000) == []


def test_a_session_that_grew_is_read_again_and_a_barely_changed_one_is_not(runner):
    """The session this rule exists for ran from 48 KB to 191 KB after being captured at entry 28."""
    capture.PROFILES = make_profiles(runner.root.parent, {"grew": 9000, "same": 4000})
    capture.DONE.write_text("grew 2026-11-09T02:00:00 alpha entries=3 prose=3000\n"
                            "same 2026-11-09T02:00:00 alpha entries=3 prose=3800\n")

    got = capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000)

    assert [c["session"] for c in got] == ["grew"]
    assert got[0]["regrew_from"] == 3000


def test_a_chatty_session_cannot_own_the_timer(runner, monkeypatch):
    """Two re-passes a day, then the session waits no matter how much it grows."""
    capture.PROFILES = make_profiles(runner.root.parent, {"chatty": 90000})
    capture.DONE.write_text("chatty 2026-11-09T02:00:00 glitch entries=3 prose=3000\n")

    assert capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000)
    capture.note_repass("chatty")
    assert capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000)
    capture.note_repass("chatty")
    assert capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000) == []


def test_the_queue_is_cheapest_first(runner):
    """Recency order put an 818 KB session ahead of a 13 KB one and starved everything behind it."""
    capture.PROFILES = make_profiles(runner.root.parent, {"big": 800000, "small": 13000})

    got = capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000)

    assert [c["session"] for c in got] == ["small", "big"]


def test_a_cron_run_is_not_memory(runner, monkeypatch):
    """58 scheduled runs sat at the head of the queue at ~5 KB each, cheaper than any real session."""
    root = make_profiles(runner.root.parent, {"job": 6000})
    capture.PROFILES = root
    c = sqlite3.connect(root / "alpha" / "state.db")
    c.execute("UPDATE messages SET content = ? WHERE session_id = 'job' AND role = 'user'",
              (capture.CRON_PREFIX + " — do the thing",))
    c.commit()
    c.close()

    assert capture.candidates(min_age_min=15, max_age_days=14, min_prose=2000) == []


def test_the_ledger_remembers_how_much_was_read(runner):
    capture.mark_done("abc", "alpha", entries=4, prose=12345)
    line = capture.DONE.read_text().strip().split()

    assert line[0] == "abc" and "prose=12345" in line and "entries=4" in line
    assert capture.ledger()["abc"] == 12345


def test_the_timer_heals_an_index_that_lost_what_the_markdown_kept(runner):
    """#128: a client holding older code blanked evidence_kind and model on every live row.

    The audit can see it because it stands on the markdown; this module is what turns that report into
    a repair without waiting for a human to run `quipu reindex`.
    """
    seed(runner, "cafe0001")
    seed(runner, "cafe0002", evidence_text="see /etc/fstab line 3")
    index_mod.reindex(runner.root, runner.db_path)
    assert capture.heal_index() == "", "a clean vault must not be rebuilt"

    c = sqlite3.connect(runner.db_path)
    c.execute("UPDATE entries SET evidence_kind = NULL, model = NULL")
    c.commit()
    c.close()

    said = capture.heal_index()

    assert said.startswith("index self-healed")
    c = sqlite3.connect(runner.db_path)
    assert c.execute("SELECT count(*) FROM entries WHERE evidence_kind != ''").fetchone()[0] == 2
    c.close()


def test_the_docstring_does_not_promise_a_tail_only_repass():
    """Measured after shipping the re-admission rule: `hook.run` deletes its chunk ledger when a pass
    completes, so re-reading a finished session costs a FULL pass (868.7 s for 253 KB). The module used
    to say otherwise, and a promise nobody can keep is worse than no promise."""
    doc = " ".join((capture.__doc__ or "").split())

    assert "FULL pass" in doc
    assert "tail only" not in doc


def test_the_hook_result_line_is_parsed_not_guessed():
    """`N entries` is what the hook reports; an older wording led with a bare number."""
    assert capture._entries_from("64 entries, 64 promoted, 0 raw in 17 chunks, 868.7s") == 64
    assert capture._entries_from("12") == 12
    assert capture._entries_from("no entries were written") == 0
