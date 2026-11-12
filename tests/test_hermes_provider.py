"""Black-box contract tests for sigil.hermes_provider (quipu issue #10).

These tests certify SigilMemoryProvider against the Hermes MemoryProvider ABC
(issue #10). The tests are the certification, not a description of the code:
they are written before the implementation exists and are expected to fail
with ImportError/ModuleNotFoundError until cipher's branch lands.

Imports stay on the public surface of ``sigil.hermes_provider``; vault
fixtures are built in tmp_path with the house idiom (sigil.config.load(...,
create=True), the ``import fabric`` CLI entry point and iter_entries — the
idiom used by tests/test_import_report.py).

Hermetic by construction: no network, nothing touched under ~/quipu-vault,
no test sleep longer than 50 ms (the queue_prefetch test fakes the *cost* of
a search at the sigil.index.search seam instead of sleeping on the happy
path; its own sleeps are 10 ms polls).

Vault resolution is certified after ``initialize(session_id, hermes_home=...)``
per the ABC's binding contract ("bind every store, path ... from
initialize()'s hermes_home; never hardcode ``~/.hermes``").

Contract point numbers (1-11 from the spec) are named in each docstring.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

import sigil.config
import sigil.index
from sigil.cli import main
from sigil.hermes_provider import SigilMemoryProvider, register
from sigil.store import iter_entries

# ---------------------------------------------------------------------------
# fixture corpus — distinctive token sets, one per entry, no cross-matching
# ---------------------------------------------------------------------------


class _Entry(NamedTuple):
    eid: str
    summary: str
    body: str


_BULK_ALPHA = (
    "moonshot archive digest filler paragraph with assorted warehouse words "
    "repeated for bulk. "
)
_BULK_BETA = (
    "telemetry archive bulk filler line with assorted sensor words for bulk. "
)

E_ALPHA = _Entry(
    eid="alpha001",
    summary="moonshot retrospective for the launch review board",
    body=(
        "The moonshot retrospective lists launch-week incidents, owners and "
        "follow-ups for the launch review board before the next orbit window."
    ),
)
E_BETA = _Entry(
    eid="beta0001",
    summary="telemetry budget audit for the fleet",
    body=(
        "The telemetry budget audit reconciles calibration drift against the "
        "fleet ledger and leaves margin notes for the next audit cycle."
    ),
)
E_GAMMA = _Entry(
    eid="gamma001",
    summary="harbor cleanup roster for the dockside crews",
    body=(
        "The harbor cleanup roster assigns dockside crews to silt removal "
        "shifts and tracks barge availability for the harbor cleanup."
    ),
)
# long entries (point 5): ~2700 words each, so a small token budget bites
E_LONG_ALPHA = _Entry(
    eid="long0001",
    summary="moonshot archive digest for cold storage",
    body=_BULK_ALPHA * 300,
)
E_LONG_BETA = _Entry(
    eid="long0002",
    summary="telemetry archive digest for cold storage",
    body=_BULK_BETA * 300,
)

ALL_THREE = [E_ALPHA, E_BETA, E_GAMMA]

MATCH_ALPHA = "moonshot retrospective launch review board"
MATCH_TELEMETRY = "telemetry budget audit"
NO_MATCH = "zzqqx wumpf blorg quib nothing here"

SESSION_ID = "qa-10-session"


# ---------------------------------------------------------------------------
# fixture helpers — vaults are always built in tmp_path, never ~/quipu-vault
# ---------------------------------------------------------------------------


def _entry_md(entry: _Entry) -> str:
    """One fabric markdown entry in the house frontmatter shape."""
    lines = [
        "---",
        f'id: "{entry.eid}"',
        'agent: "vector"',
        'platform: "cli"',
        'timestamp: "2026-10-30T12:00:00Z"',
        'type: "decision"',
        'tier: "hot"',
        f"summary: {json.dumps(entry.summary)}",
        'project_id: "quipu"',
        'status: "completed"',
        f'outcome: "recorded: {entry.eid}"',
        'evidence: "pytest -q: ok"',
        'training_value: "high"',
        "---",
        "",
        entry.body,
    ]
    return "\n".join(lines) + "\n"


def build_vault(path: Path, entries: list[_Entry]) -> Path:
    """Create a real vault at *path* (under tmp_path) holding *entries*."""
    sigil.config.load(path, create=True)
    src = path.parent / (path.name + ".src")
    src.mkdir(parents=True, exist_ok=True)
    for i, entry in enumerate(entries):
        (src / f"{i:02d}.md").write_text(_entry_md(entry), encoding="utf-8")
    exit_code = main(["--vault", str(path), "import", "fabric", str(src)])
    assert exit_code == 0, f"fixture import failed with exit code {exit_code}"
    return path


def count_entries(vault: Path) -> int:
    """House counter over the vault's stored entries."""
    return len(list(iter_entries(vault)))


def _make_provider(tmp_path: Path, monkeypatch, entries: list[_Entry], vault_name="vault"):
    """Provider pointed at a fresh tmp vault via QUIPU_VAULT, then initialized."""
    vault = build_vault(tmp_path / vault_name, entries)
    monkeypatch.delenv("QUIPU_VAULT", raising=False)
    monkeypatch.setenv("QUIPU_VAULT", str(vault))
    provider = SigilMemoryProvider()
    provider.initialize(
        SESSION_ID,
        hermes_home=str(tmp_path / ".hermes-not-used"),
        platform="cli",
    )
    return provider, vault


# ---------------------------------------------------------------------------
# point 1 — the interface is complete
# ---------------------------------------------------------------------------


def test_interface_is_complete():
    """An instance exposes every contracted attribute; the name follows the registration."""
    provider = SigilMemoryProvider()
    assert provider.name == "sigil"
    for attr in (
        "is_available",
        "initialize",
        "system_prompt_block",
        "prefetch",
        "queue_prefetch",
        "sync_turn",
        "get_tool_schemas",
        "handle_tool_call",
        "shutdown",
        "unavailable_reason",
    ):
        assert callable(getattr(provider, attr)), attr


# ---------------------------------------------------------------------------
# point 2 — vault resolution order (each case in its own tmp dirs)
# ---------------------------------------------------------------------------


def test_vault_resolution_uses_quipu_vault_env(tmp_path, monkeypatch):
    """QUIPU_VAULT set to a directory containing .sigil/index.db wins."""
    vault = build_vault(tmp_path / "env-vault", [E_ALPHA])
    monkeypatch.delenv("QUIPU_VAULT", raising=False)
    monkeypatch.setenv("QUIPU_VAULT", str(vault))
    provider = SigilMemoryProvider()
    provider.initialize(SESSION_ID, hermes_home=str(tmp_path / "no-homes-here"), platform="cli")
    assert provider.is_available()
    assert str(vault) in provider.system_prompt_block()
    assert "moonshot" in provider.prefetch(MATCH_ALPHA)


def test_vault_resolution_falls_back_to_hermes_home_vault(tmp_path, monkeypatch):
    """No QUIPU_VAULT: the hermes_home/quipu-vault vault is used when it exists."""
    monkeypatch.delenv("QUIPU_VAULT", raising=False)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    home_vault = build_vault(hermes_home / "quipu-vault", [E_BETA])
    provider = SigilMemoryProvider()
    provider.initialize(SESSION_ID, hermes_home=str(hermes_home), platform="cli")
    assert provider.is_available()
    assert str(home_vault) in provider.system_prompt_block()
    assert "telemetry" in provider.prefetch(MATCH_TELEMETRY)


def test_vault_resolution_without_any_vault_reports_reason(tmp_path, monkeypatch):
    """Nothing to resolve: unavailable, with a reason naming the paths it looked for."""
    monkeypatch.delenv("QUIPU_VAULT", raising=False)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()  # hermes home exists, but holds no quipu-vault
    # The third candidate in the documented order is `~/quipu-vault`, and on this box that vault
    # exists — so the test must move HOME into the tmp tree or it asserts against the live vault.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    provider = SigilMemoryProvider()
    provider.initialize(SESSION_ID, hermes_home=str(hermes_home), platform="cli")
    assert not provider.is_available()
    reason = provider.unavailable_reason()
    assert isinstance(reason, str)
    assert reason.strip(), "unavailable_reason must be a non-empty string"
    assert "quipu-vault" in reason, "reason must name the paths it looked for"


# ---------------------------------------------------------------------------
# point 3 — system_prompt_block is small and carries no recalled entries
# ---------------------------------------------------------------------------


def test_system_prompt_block_small_and_points_at_vault(tmp_path, monkeypatch):
    """Non-empty, <= 150 whitespace tokens, contains the vault path, no recall."""
    provider, vault = _make_provider(tmp_path, monkeypatch, [E_ALPHA, E_BETA])
    block = provider.system_prompt_block()
    assert block.strip()
    assert len(block.split()) <= 150
    assert str(vault) in block
    assert "moonshot" not in block, "system prompt must not carry recalled entries"


# ---------------------------------------------------------------------------
# point 4 — prefetch is a search: matches, empty miss, no network
# ---------------------------------------------------------------------------


def test_prefetch_returns_only_the_matching_entry(tmp_path, monkeypatch):
    """A query matching one of three entries returns that entry, not the others."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)
    result = provider.prefetch(MATCH_ALPHA)
    assert "moonshot" in result
    assert "telemetry" not in result
    assert "harbor" not in result


def test_prefetch_with_no_match_returns_empty(tmp_path, monkeypatch):
    """A query matching nothing returns '' (not an error, not junk)."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)
    assert provider.prefetch(NO_MATCH) == ""


def test_prefetch_makes_no_network_calls(tmp_path, monkeypatch):
    """With sockets disabled entirely, prefetch still returns the match."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)

    def _forbidden(*args, **kwargs):
        raise AssertionError("prefetch reached for the network")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    result = provider.prefetch(MATCH_ALPHA)
    assert "moonshot" in result


# ---------------------------------------------------------------------------
# point 5 — prefetch obeys its token budget
# ---------------------------------------------------------------------------


def test_prefetch_respects_token_budget(tmp_path, monkeypatch):
    """A large vault queried under a 40-token budget stays within it."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, [E_LONG_ALPHA, E_LONG_BETA])
    result = provider.prefetch("moonshot archive digest for cold storage", max_tokens=40)
    assert result.strip(), "budgeted prefetch must still return the match"
    assert len(result.split()) <= 40


# ---------------------------------------------------------------------------
# point 6 — queue_prefetch does not block and serves the cache next turn
# ---------------------------------------------------------------------------


def test_queue_prefetch_nonblocking_and_cached(tmp_path, monkeypatch):
    """queue_prefetch returns well under the cost of a search; prefetch next
    turn serves the cached result."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)

    def _slow(fn):
        def wrapper(*args, **kwargs):
            time.sleep(0.10)  # the simulated cost of a search (not a test sleep)
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(sigil.index, "search", _slow(sigil.index.search))

    # instrument check: an uncached search really is slowed down
    t_cold = time.perf_counter()
    cold = provider.prefetch(MATCH_TELEMETRY)
    cold_dt = time.perf_counter() - t_cold
    assert "telemetry" in cold
    assert cold_dt >= 0.08, f"search seam not slowed ({cold_dt:.3f}s) - patch the right seam"

    # queue_prefetch must return immediately, well under the search's cost
    t_queue = time.perf_counter()
    provider.queue_prefetch(MATCH_ALPHA)
    queue_dt = time.perf_counter() - t_queue
    assert queue_dt < 0.05, f"queue_prefetch blocked for {queue_dt:.3f}s"

    # next turn: the cached result comes back fast and correct
    cached, cached_dt = "", 99.0
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        t1 = time.perf_counter()
        cached = provider.prefetch(MATCH_ALPHA)
        cached_dt = time.perf_counter() - t1
        if cached_dt < 0.08:
            break
        time.sleep(0.01)
    assert cached_dt < 0.08, "the queued result was never served from cache"
    assert "moonshot" in cached
    assert "telemetry" not in cached
    assert "harbor" not in cached


# ---------------------------------------------------------------------------
# point 7 — sync_turn appends the session journal, it does not create entries
# ---------------------------------------------------------------------------


def test_sync_turn_journal_appends_without_creating_entries(tmp_path, monkeypatch):
    """Three sync_turns: entry count unchanged, session-<id>.jsonl has 3 lines."""
    provider, vault = _make_provider(tmp_path, monkeypatch, [E_ALPHA])
    before = count_entries(vault)
    turns = [
        ("who ran the moonshot retro?", "the launch review board did"),
        ("second question of the session", "second answer of the session"),
        ("third question of the session", "third answer of the session"),
    ]
    for user_content, assistant_content in turns:
        provider.sync_turn(user_content=user_content, assistant_content=assistant_content)
    provider.shutdown()  # flush any queued writer before reading (ABC lifecycle)

    assert count_entries(vault) == before, "sync_turn must not create entries"
    journals = sorted((vault / ".sigil").glob("session-*.jsonl"))
    assert len(journals) == 1
    assert journals[0].stem == f"session-{SESSION_ID}"
    lines = journals[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    for turn in parsed:
        assert {"ts", "session", "user", "assistant"} <= set(turn)
    assert [turn["user"] for turn in parsed] == [u for u, _a in turns]
    assert [turn["assistant"] for turn in parsed] == [a for _u, a in turns]
    assert all(turn["session"] == SESSION_ID for turn in parsed)


# ---------------------------------------------------------------------------
# point 8 — sync_turn never raises and never writes outside the vault
# ---------------------------------------------------------------------------


def test_sync_turn_never_raises_and_stays_inside_vault(tmp_path, monkeypatch):
    """None-ish and very long content: no exception, nothing outside the vault."""
    provider, vault = _make_provider(tmp_path, monkeypatch, [E_ALPHA])
    before_cwd = {p.name for p in Path.cwd().iterdir()}
    before_tmp = {p.name for p in tmp_path.iterdir()}
    before_count = count_entries(vault)

    for weird in (None, "", "x" * 200_000):
        provider.sync_turn(user_content=weird, assistant_content=weird)
    provider.shutdown()

    assert count_entries(vault) == before_count
    assert {p.name for p in Path.cwd().iterdir()} == before_cwd
    assert {p.name for p in tmp_path.iterdir()} == before_tmp


# ---------------------------------------------------------------------------
# point 9 — get_tool_schemas
# ---------------------------------------------------------------------------


def test_get_tool_schemas_shape_and_exact_names(tmp_path, monkeypatch):
    """Two schemas, dicts with name/description/parameters, exact name set."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, [E_ALPHA])
    schemas = provider.get_tool_schemas()
    assert isinstance(schemas, list)
    assert len(schemas) == 2
    for schema in schemas:
        assert isinstance(schema, dict)
        assert isinstance(schema["name"], str) and schema["name"].strip()
        assert isinstance(schema["description"], str) and schema["description"].strip()
        assert isinstance(schema["parameters"], dict)
    assert sorted(s["name"] for s in schemas) == ["sigil_search", "sigil_write"]


# ---------------------------------------------------------------------------
# point 10 — handle_tool_call always answers with a JSON string
# ---------------------------------------------------------------------------


def test_handle_tool_call_returns_json_string_for_known_tools(tmp_path, monkeypatch):
    """Both tools answer with a string that parses to a JSON object."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)
    for tool_name, args in (
        ("sigil_search", {"query": MATCH_ALPHA}),
        ("sigil_write", {"summary": "qa probe summary for the write tool"}),
    ):
        raw = provider.handle_tool_call(tool_name, args)
        assert isinstance(raw, str), tool_name
        assert isinstance(json.loads(raw), dict), tool_name


def test_handle_tool_call_sigil_search_finds_the_entry(tmp_path, monkeypatch):
    """The search tool actually surfaces the vault's matching entry."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, ALL_THREE)
    raw = provider.handle_tool_call("sigil_search", {"query": MATCH_ALPHA})
    assert isinstance(raw, str)
    assert "moonshot" in raw


def test_handle_tool_call_unknown_tool_returns_error_json(tmp_path, monkeypatch):
    """An unknown tool name yields a JSON object with an 'error' key, no raise."""
    provider, _vault = _make_provider(tmp_path, monkeypatch, [E_ALPHA])
    raw = provider.handle_tool_call("sigil_explode", {})
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "error" in parsed


# ---------------------------------------------------------------------------
# point 11 — register(ctx) registers exactly one provider
# ---------------------------------------------------------------------------


def test_register_registers_exactly_one_provider():
    """register(ctx) hands exactly one fresh provider to the plugin context."""
    calls = []

    class RecordingCtx:
        def register_memory_provider(self, *args, **kwargs):
            calls.append((args, kwargs))

    register(RecordingCtx())
    assert len(calls) == 1
    args, kwargs = calls[0]
    provider = args[0] if args else kwargs.get("provider")
    assert getattr(provider, "name", None) == "sigil"