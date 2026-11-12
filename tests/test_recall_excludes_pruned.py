"""Pruned entries are history, not recall.

`state: pruned` is the vault's judgement that an entry has no recall or training value — a session
status paste, a duplicated handoff, a transcript dump. Until 0.10.1 pruning changed the exports and the
graph and nothing else: `index.search()` filtered on state only when the caller passed one, so a pruned
entry came back at rank 1 for a query that matched its own words (measured on the live vault,
2026-11-12: four "here's where things stand" notes, pruned, ranked 1-3 for their own text).

These are the regression tests for the filter at all three entry points: the index, the CLI, and the
MCP tool. The graph and the exports still read pruned entries — this is about what retrieval offers.
"""

from sigil import index as index_mod
from sigil import mcp, store
from sigil.cli import main
from sigil.store import Entry

DUMP = "status paste nothing is running here"
CLAIM = "a promoted claim that also mentions nothing running here"


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def seed(capsys, vault):
    """One pruned dump and one promoted claim that share the query's words."""
    run(capsys, "--vault", vault, "init")
    assert run(capsys, "--vault", vault, "write", "--kind", "fact", "--state", "pruned",
               "--summary", DUMP, "--evidence", "session dump", "--project", "ops")[0] == 0
    assert run(capsys, "--vault", vault, "write", "--kind", "decision", "--state", "promoted",
               "--summary", CLAIM, "--evidence", "measured in a test", "--project", "ops")[0] == 0


def test_search_hides_pruned_entries(cfg, capsys):
    v = str(cfg.root)
    seed(capsys, v)
    code, out = run(capsys, "--vault", v, "search", "nothing running")
    assert code == 0, "the promoted claim still matches, so search must succeed"
    assert CLAIM in out
    assert DUMP not in out, "a pruned entry must not be offered as recall"


def test_a_pruned_only_vault_searches_empty(cfg, capsys):
    v = str(cfg.root)
    run(capsys, "--vault", v, "init")
    assert run(capsys, "--vault", v, "write", "--kind", "fact", "--state", "pruned",
               "--summary", DUMP, "--evidence", "session dump")[0] == 0
    code, out = run(capsys, "--vault", v, "search", "nothing running")
    assert code == 1, "no rows is the CLI's no-matches exit, not a crash"
    assert DUMP not in out


def test_an_explicit_state_filter_still_reaches_pruned(cfg, capsys):
    v = str(cfg.root)
    seed(capsys, v)
    code, out = run(capsys, "--vault", v, "search", "nothing running", "--state", "pruned")
    assert code == 0
    assert DUMP in out, "asking for pruned explicitly is how an audit reaches them"
    assert CLAIM not in out


def test_all_states_brings_them_back(cfg, capsys):
    v = str(cfg.root)
    seed(capsys, v)
    code, out = run(capsys, "--vault", v, "search", "nothing running", "--all-states")
    assert code == 0
    assert DUMP in out and CLAIM in out


def test_the_index_default_is_exclusion(cfg):
    for state, summary in (("pruned", DUMP), ("promoted", CLAIM)):
        store.write_entry(cfg.root, Entry(kind="fact", state=state, summary=summary,
                                          evidence="written by the test", importance=0.9))
    index_mod.reindex(cfg.root, cfg.db_path)

    hidden = index_mod.search(cfg.db_path, "nothing running")
    assert [r["summary"] for r in hidden] == [CLAIM]

    asked = index_mod.search(cfg.db_path, "nothing running", include_pruned=True)
    assert {r["summary"] for r in asked} == {DUMP, CLAIM}

    state_only = index_mod.search(cfg.db_path, "nothing running", state="pruned")
    assert [r["summary"] for r in state_only] == [DUMP]


def test_the_mcp_search_tool_defaults_to_exclusion(cfg):
    for state, summary in (("pruned", DUMP), ("promoted", CLAIM)):
        store.write_entry(cfg.root, Entry(kind="fact", state=state, summary=summary,
                                          evidence="written by the test", importance=0.9))
    index_mod.reindex(cfg.root, cfg.db_path)

    payload = mcp._search(cfg, {"query": "nothing running"})
    assert [hit["summary"] for hit in payload["hits"]] == [CLAIM]

    asked = mcp._search(cfg, {"query": "nothing running", "include_pruned": True})
    assert {hit["summary"] for hit in asked["hits"]} == {DUMP, CLAIM}
    # the tool advertises the flag, so a caller can discover it without reading the source
    schema = next(tool for tool in mcp.TOOLS if tool["name"] == "search")
    assert "include_pruned" in schema["inputSchema"]["properties"]


def test_the_provider_recall_does_not_inject_pruned_text(cfg, monkeypatch):
    """The injected block is what teaches the agent to distrust recall, so pin it directly."""
    from sigil import hermes_provider as provider_mod

    for state, summary in (("pruned", DUMP), ("promoted", CLAIM)):
        store.write_entry(cfg.root, Entry(kind="fact", state=state, summary=summary,
                                          evidence="written by the test", importance=0.9))
    index_mod.reindex(cfg.root, cfg.db_path)

    monkeypatch.setenv("SIGIL_VAULT", str(cfg.root))
    provider = provider_mod.SigilMemoryProvider(config={"vault": str(cfg.root)})
    provider.initialize("test-session")
    try:
        block = provider.prefetch("nothing running")
    finally:
        provider.shutdown()

    assert CLAIM in block, "recall must still surface the promoted claim"
    assert DUMP not in block, "pruned text must never reach the prompt"
