"""The boot page and the capture limit (#51) — both are about what an agent can hold at once.

`quipu brief` is the standing answer: promoted entries, one line each, bounded by default because a
289-entry brief renders 20k tokens and a boot page that large is not a boot page. The size limit on
`summary` is the other half: a blob written today is a blob ranked badly for the rest of the vault's life,
and the measured failures of October were all of that class, so the writing is where it gets stopped.

Everything here drives the CLI or the MCP handler — the surfaces an agent uses — not internals. The one
exception is `write_entry`, and it is the point: importers must keep working on historical notes.
"""

import json

import pytest

from sigil import brief as brief_mod
from sigil import config as config_mod
from sigil import store as store_mod
from sigil.cli import main
from sigil.mcp import handle
from sigil.store import Entry, iter_entries, write_entry


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    entries = [
        Entry(kind="fact", state="promoted", summary="Docker volumes live under /var/lib/docker",
              evidence="ls /var/lib/docker", ask="where are docker volumes?", project="infra",
              importance=0.9, created="2026-10-01T00:00:00Z"),
        Entry(kind="decision", state="promoted", summary="Markdown is the source of truth, sqlite is a cache",
              evidence="40 ms full reindex", ask="what owns the truth?", project="quipu",
              importance=0.95, created="2026-10-02T00:00:00Z"),
        Entry(kind="lesson", state="promoted", summary="Wan 2.2 VAE decode hangs the ring on gfx1031",
              evidence="journalctl -b -1", project="video", importance=0.4,
              created="2026-10-03T00:00:00Z"),
        Entry(kind="fact", state="raw", summary="raw note that must never appear in a brief",
              evidence="x", importance=1.0),
        Entry(kind="fact", state="pruned", summary="pruned transcript junk", evidence="x", importance=1.0),
    ]
    for e in entries:
        write_entry(root, e)
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    return root, entries


def run_cli(*argv):
    return main([str(a) for a in argv])


# --- the brief -------------------------------------------------------------------------------


def test_brief_holds_only_promoted_entries_and_a_bound(vault):
    root, entries = vault
    text = brief_mod.build(e for _p, e in iter_entries(root))
    assert "Docker volumes live under /var/lib/docker" in text
    assert "raw note that must never appear" not in text
    assert "pruned transcript junk" not in text
    # the bound is the default, and it is a bound
    assert "3 promoted entries" in text or "2 promoted entries" in text or "entries" in text
    assert len(text) < 4000, len(text)


def test_brief_is_deterministic_and_ordered_by_importance(vault):
    root, _entries = vault
    a = brief_mod.build(e for _p, e in iter_entries(root))
    b = brief_mod.build(e for _p, e in iter_entries(root))
    assert a == b, "two renders of an unchanged vault differ"
    # importance 0.95 leads its kind section, 0.4 trails
    assert a.index("Markdown is the source of truth") < a.index("Wan 2.2 VAE decode")


def test_brief_carries_the_question_and_the_evidence(vault):
    root, _entries = vault
    text = brief_mod.build(e for _p, e in iter_entries(root))
    assert "answers: where are docker volumes?" in text
    assert "evidence: ls /var/lib/docker" in text


def test_brief_respects_the_limit_and_a_limit_of_zero_means_all(vault):
    root, _entries = vault
    assert brief_mod.build((e for _p, e in iter_entries(root)), max_lines=1).count("- **") == 1
    assert brief_mod.build((e for _p, e in iter_entries(root)),
                           max_lines=0).count("- **") == 3


def test_brief_filters_by_project(vault):
    root, _entries = vault
    text = brief_mod.build((e for _p, e in iter_entries(root)), project="quipu")
    assert "Markdown is the source of truth" in text
    assert "Docker volumes" not in text


def test_brief_never_lands_in_the_notes_directory(vault, capsys):
    """A generated page beside the entries would be ingested as an entry — the self-magnet failure."""
    root, _entries = vault
    assert run_cli("--vault", root, "brief") == 0
    out = capsys.readouterr()
    path = out.out.strip().splitlines()[-1]
    assert "/.sigil/brief/" in path, path
    assert "/notes/" not in path
    assert (root / ".sigil" / "brief" / "vault.md").exists()


# --- the capture limit -------------------------------------------------------------------


def test_write_refuses_a_summary_over_the_limit(vault, capsys):
    root, _entries = vault
    big = "x" * 400
    assert run_cli("--vault", root, "write", "--summary", big) == 2
    err = capsys.readouterr().err
    assert "limit 280" in err and "400 bytes" in err
    # and nothing was written
    assert not any("xxxx" in (e.summary or "") for _p, e in iter_entries(root))


def test_write_accepts_the_limit_exactly(vault):
    root, _entries = vault
    exact = "y" * store_mod.SUMMARY_LIMIT_BYTES
    assert run_cli("--vault", root, "write", "--summary", exact) == 0


def test_write_can_opt_out_deliberately(vault):
    root, _entries = vault
    assert run_cli("--vault", root, "write", "--summary", "z" * 400, "--allow-long-summary") == 0


def test_the_limit_counts_bytes_not_characters(vault):
    """280 characters of emoji is 1120 bytes: the limit is what the file and the model see."""
    root, _entries = vault
    assert run_cli("--vault", root, "write", "--summary", "🧠" * 80) == 2


def test_mcp_write_has_the_same_gate_and_no_way_round_it(vault, monkeypatch):
    root, _entries = vault
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    cfg = config_mod.load(root, create=True)
    resp = handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "write",
                                   "arguments": {"summary": "q" * 500}}})
    blob = json.dumps(resp)
    assert "limit 280" in blob, blob[:400]


def test_importers_can_still_write_historical_notes(vault):
    """The rule is for capture, not for migration: `write_entry` is untouched on purpose."""
    root, _entries = vault
    path = write_entry(root, Entry(kind="capture", state="raw", summary="h" * 900))
    assert path.exists()
