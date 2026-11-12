"""A search that can be restricted to a time window (#52).

Entries have always carried `created`; nothing ever filtered on it, so "what did we decide in October"
returned the best lexical matches of all time and left the caller reading dates off the results.

The rule that earns these tests is the bare-date one: `--until 2026-10-29` means *through* the 29th,
not "before midnight on the 29th". Everything in a working vault is created in the afternoon, so the
off-by-one-day version of this flag returns nothing and looks like a broken query rather than a wrong
bound. Driven through the CLI and the MCP handler — the surfaces an agent actually uses.
"""

import json

import pytest

from sigil import config as config_mod
from sigil import index as index_mod
from sigil.cli import main
from sigil.mcp import handle
from sigil.store import Entry, write_entry


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    entries = [
        Entry(kind="fact", state="promoted", summary="alpha note about the glitch box",
              created="2026-09-30T23:59:00Z", evidence="x"),
        Entry(kind="fact", state="promoted", summary="alpha note decided on the first of october",
              created="2026-10-01T00:00:00Z", evidence="x"),
        Entry(kind="fact", state="promoted", summary="alpha note in the middle of october",
              created="2026-10-15T14:30:00Z", evidence="x"),
        Entry(kind="fact", state="promoted", summary="alpha note late on the 29th",
              created="2026-10-29T23:59:00Z", evidence="x"),
        Entry(kind="fact", state="promoted", summary="alpha note in november",
              created="2026-11-02T09:00:00Z", evidence="x"),
    ]
    for e in entries:
        write_entry(root, e)
    index_mod.reindex(root, root / ".sigil" / "index.db")
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    return root


def ids(root, **kw):
    return [r["created"] for r in index_mod.search(root / ".sigil" / "index.db", "alpha", **kw)]


def test_bare_since_date_starts_at_midnight_utc(vault):
    got = ids(vault, since="2026-10-01")
    assert "2026-10-01T00:00:00Z" in got, got
    assert "2026-09-30T23:59:00Z" not in got, got


def test_bare_until_date_runs_through_the_whole_day(vault):
    """The defect this exists for: `until 2026-10-29` must include a note made at 23:59 that day."""
    got = ids(vault, until="2026-10-29")
    assert "2026-10-29T23:59:00Z" in got, got
    assert "2026-11-02T09:00:00Z" not in got, got


def test_both_bounds_together(vault):
    # ranked by relevance, not chronology — assert the SET of survivors, not the order
    got = ids(vault, since="2026-10-01", until="2026-10-29")
    assert sorted(got) == ["2026-10-01T00:00:00Z", "2026-10-15T14:30:00Z", "2026-10-29T23:59:00Z"], got


def test_explicit_instants_are_exclusive_at_until(vault):
    got = ids(vault, since="2026-10-01T00:00:00Z", until="2026-10-15T14:30:00Z")
    assert "2026-10-15T14:30:00Z" not in got, got
    assert got == ["2026-10-01T00:00:00Z"], got


def mcp_text(resp):
    """MCP wraps tool payloads in a content block; parse the JSON back out of it."""
    return json.loads(resp["result"]["content"][0]["text"])


def test_offset_instants_are_normalised_to_utc(vault):
    # 2026-10-15T10:30:00-04:00 is 14:30Z, the instant of the middle note
    got = ids(vault, since="2026-10-15T10:30:00-04:00", until="2026-10-15T10:31:00-04:00")
    assert got == ["2026-10-15T14:30:00Z"], got


def test_a_window_that_excludes_everything_returns_nothing_not_an_error(vault):
    assert ids(vault, since="2027-01-01") == []


def test_an_unparseable_bound_is_a_usage_error_naming_the_value(vault, capsys):
    assert main(["--vault", str(vault), "search", "alpha", "--since", "october"]) == 2
    err = capsys.readouterr().err
    assert "october" in err and "not a date" in err, err


def test_the_window_combines_with_the_other_filters(vault):
    root = vault
    got = ids(root, since="2026-10-01", state="promoted")
    assert len(got) == 4, got
    assert ids(root, since="2026-10-01", state="raw") == []


def test_time_bound_rejects_an_empty_value():
    with pytest.raises(ValueError):
        index_mod.time_bound("", upper=False)


def test_mcp_search_carries_the_window(vault, monkeypatch):
    root = vault
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    cfg = config_mod.load(root, create=True)
    resp = handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "search",
                                   "arguments": {"query": "alpha", "until": "2026-10-29"}}})
    created = [h["created"] for h in mcp_text(resp)["hits"]]
    assert "2026-10-29T23:59:00Z" in created, created
    assert "2026-11-02T09:00:00Z" not in created, created


def test_mcp_search_reports_a_bad_window_instead_of_ignoring_it(vault, monkeypatch):
    root = vault
    monkeypatch.setenv("QUIPU_VAULT", str(root))
    cfg = config_mod.load(root, create=True)
    resp = json.dumps(handle(cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "search",
                                             "arguments": {"query": "alpha", "since": "nope"}}}))
    assert "since/until" in resp, resp[:400]
