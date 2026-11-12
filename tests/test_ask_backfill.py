"""`quipu ask-backfill` — the field that makes an entry trainable (#27).

`ask` is the question a searcher would ask; without it an entry is recall only and `quipu export` has no
pair to build. Only 17 of 864 entries had one. This is the only place quipu calls a model, so the tests are
about the guard rails around that call rather than the call itself: every answer is validated before it is
stored, the write never renames a note, and a dry run touches nothing.

The model is never contacted here — `ask_for` is patched — so the suite stays hermetic and offline.
"""

import textwrap

import pytest

from sigil import askfill
from sigil.cli import main

SUMMARY = "Fabric audit (502 entries): 0% linking, 85% bad summaries, 90% wrong agent"
GOOD = "What did the 502-entry fabric audit show about linking and wrong agent identities?"


def write_note(root, eid, summary, ask="", body="body"):
    path = root / "notes" / f"{eid}-{eid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    ask_line = f'ask: "{ask}"\n' if ask else ""
    path.write_text(textwrap.dedent(f"""\
        ---
        id: {eid}
        kind: fact
        state: promoted
        created: 2026-10-01T10:00:00Z
        updated: 2026-10-01T10:00:00Z
        summary: "{summary}"
        {ask_line}evidence: "x"
        ---

        """) + body + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    write_note(root, "aaaa1111", SUMMARY)
    write_note(root, "bbbb2222", "already has a question", ask="What is already asked?")
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    return root


# --- validation ------------------------------------------------------------------------------


@pytest.mark.parametrize("ask", [
    GOOD,
    "Why does the importer keep single quotes on folded YAML summaries?",
])
def test_a_real_question_passes(ask):
    assert askfill.validate(ask, SUMMARY) == ""


@pytest.mark.parametrize("ask,why", [
    ("", "empty"),
    ("the fabric audit showed 0% linking", "no question mark at all"),
    ("What linking?", "too short to be a search handle"),
    (GOOD.replace("?", ""), "missing the question mark"),
    ("NONE", "the model declined"),
])
def test_bad_answers_are_rejected_with_a_reason(ask, why):
    assert askfill.validate(ask, SUMMARY) != "", why


def test_a_question_that_just_restates_the_summary_is_rejected():
    echo = SUMMARY.replace(":", "") + "?"
    assert "restates" in askfill.validate(echo, SUMMARY)


def test_an_overlong_question_is_rejected():
    assert "too long" in askfill.validate("What " + "x" * 300 + "?", SUMMARY)


# --- cleaning --------------------------------------------------------------------------------


def test_a_label_prefix_and_quotes_are_stripped():
    assert askfill._clean('Question: "What did the audit find?"') == "What did the audit find?"


def test_a_question_shaped_line_missing_its_mark_gets_one():
    assert askfill._clean("What did the audit find about linking") == \
        "What did the audit find about linking?"


def test_a_statement_without_an_interrogative_is_not_forced_into_a_question():
    assert askfill._clean("The audit found 0% linking") == "The audit found 0% linking"


# --- running and writing ---------------------------------------------------------------------


def test_only_entries_without_an_ask_are_candidates(vault):
    ids = [e.id for _p, e in askfill.candidates(vault)]
    assert ids == ["aaaa1111"], ids


def test_run_proposes_and_counts_without_writing(vault, monkeypatch):
    monkeypatch.setattr(askfill, "ask_for", lambda entry, url, model, api_key="", timeout=0: GOOD)
    result = askfill.run(vault, "http://x/v1/chat/completions", "m")
    assert [p.ask for p in result.proposals] == [GOOD]
    assert result.written == 0
    assert askfill.candidates(vault), "a dry run wrote an ask"


def test_a_rejected_answer_costs_the_entry_and_nothing_else(vault, monkeypatch):
    monkeypatch.setattr(askfill, "ask_for", lambda *a, **k: "The audit found 0% linking")
    result = askfill.run(vault, "http://x/v1/chat/completions", "m")
    assert result.skipped == 1 and result.proposals[0].reason
    askfill.apply(vault, result)
    assert "ask:" not in next((vault / "notes").glob("aaaa1111-*.md")).read_text()


def test_an_endpoint_error_is_counted_not_raised(vault, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("nope")
    monkeypatch.setattr(askfill, "ask_for", boom)
    result = askfill.run(vault, "http://x/v1/chat/completions", "m")
    assert result.failed == 1


def test_apply_writes_the_ask_in_place_and_keeps_the_path(vault, monkeypatch):
    monkeypatch.setattr(askfill, "ask_for", lambda *a, **k: GOOD)
    names = sorted(p.name for p in (vault / "notes").glob("*.md"))
    result = askfill.run(vault, "http://x/v1/chat/completions", "m")
    assert askfill.apply(vault, result) == 1
    path = next((vault / "notes").glob("aaaa1111-*.md"))
    assert GOOD in path.read_text()
    assert sorted(p.name for p in (vault / "notes").glob("*.md")) == names, "apply renamed a note"
    # and the entry is no longer a candidate
    assert [e.id for _p, e in askfill.candidates(vault)] == []


def test_cli_dry_run_writes_nothing_and_reports(vault, monkeypatch, capsys):
    monkeypatch.setattr(askfill, "ask_for", lambda *a, **k: GOOD)
    assert main(["--vault", str(vault), "ask-backfill"]) == 0
    out = capsys.readouterr().out
    assert '"written": 0' in out, out
    assert "ask:" not in next((vault / "notes").glob("aaaa1111-*.md")).read_text()


def test_cli_count_reports_the_gap(vault, capsys):
    assert main(["--vault", str(vault), "ask-backfill", "--count"]) == 0
    assert '"candidates": 1' in capsys.readouterr().out
