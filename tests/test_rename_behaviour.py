"""The behaviour that still carried the old name, and the failure each fix removes.

The rename moved the package; the things that stayed behind were not strings but ROUTES:

- a vault created after the rename writes its chunk ledger into `.quipu/chunks`, a directory the
  rest of the product never looks in;
- the capture unit prints and logs under a command that no longer exists;
- the benchmark harness reads `.quipu/index.db`, so against a renamed vault it finds no index and
  reports zeros — a published measurement that is really a wrong path;
- the Hermes provider calls itself quipu while Hermes registers it as sigil;
- the messages a user reads name binaries and paths that are gone.

Every test here is against behaviour, not against a source string, except where the surface IS a
string (a --help line, a log prefix). Nothing here touches the network or the real vault.
"""
from __future__ import annotations

import fcntl
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import vault as bench_vault
from sigil import brief as brief_mod
from sigil import capture as capture_mod
from sigil import graph as graph_mod
from sigil import hermes_provider as provider_mod
from sigil import hook as hook_mod
from sigil import mcp as mcp_mod
from sigil import promote as promote_mod
from sigil.config import LEGACY_META_DIR, load
from sigil.store import Entry

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1 — the chunk ledger follows the vault's own meta dir
# ---------------------------------------------------------------------------


def _proposal():
    return {
        "kind": "conclusion",
        "summary": "the hook wrote this entry at session end",
        "evidence": "pytest run of tests/test_rename_behaviour.py",
        "importance": 0.9,
        "ask": "what does the session-end hook write?",
        "entities": [],
        "edges": [],
        "confidence": 0.8,
    }


def _fake_provider():
    return promote_mod.FakeProvider([_proposal()])


def _transcript(tmp_path):
    path = tmp_path / "session.md"
    path.write_text("user: we fixed the ledger path\nassistant: entries written\n", encoding="utf-8")
    return path


def test_a_fresh_vault_puts_the_chunk_ledger_in_its_own_meta_dir(tmp_path):
    cfg = load(tmp_path / "vault", create=True)

    path = hook_mod._ledger_path(cfg, "session-1")

    assert path.parent.parent.name == ".sigil", f"the ledger must live in the vault's meta dir: {path}"
    assert not (cfg.root / LEGACY_META_DIR).exists()


def test_a_hook_run_leaves_no_legacy_directory_in_a_fresh_vault(tmp_path):
    """The failure this removes: a fresh vault grows a `.quipu` directory it can never read back."""
    cfg = load(tmp_path / "vault", create=True)

    result = hook_mod.run(cfg, _transcript(tmp_path), _fake_provider(), agent="cipher", project="sigil")

    assert result.written, "the pass must have written its entry"
    assert not (cfg.root / LEGACY_META_DIR).exists(), "a fresh vault must not gain a .quipu directory"
    assert (cfg.root / ".sigil" / "chunks").exists(), "the ledger must be written inside .sigil"


def test_a_legacy_vault_keeps_its_ledger_where_it_already_is(tmp_path):
    """A vault written before the rename must not be moved by the fix."""
    root = tmp_path / "old-vault"
    meta = root / LEGACY_META_DIR
    meta.mkdir(parents=True)
    (meta / "config.toml").write_text("[quipu]\n", encoding="utf-8")
    cfg = load(root)

    path = hook_mod._ledger_path(cfg, "session-1")

    assert path.parent.parent == meta, "a legacy vault keeps its ledger where it was"


# ---------------------------------------------------------------------------
# 2 — the capture unit names the command that exists
# ---------------------------------------------------------------------------


def test_the_capture_log_is_written_under_the_current_name():
    assert capture_mod.LOG_PATH.name == "sigil-capture.log"


def test_capture_help_prints_the_current_command():
    out = subprocess.run([sys.executable, "-m", "sigil.capture", "--help"],
                         capture_output=True, text=True, timeout=60)

    assert "usage: sigil-capture" in out.stdout, out.stdout
    assert "quipu-capture" not in out.stdout


def test_the_lock_message_names_the_current_command(tmp_path, monkeypatch, capsys):
    """A second run must say which command holds the lock, and that command must exist."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(capture_mod, "STATE_DIR", state)
    monkeypatch.setattr(capture_mod, "LOG", tmp_path / "capture.log")
    monkeypatch.setattr(capture_mod, "VAULT", tmp_path / "vault")
    (tmp_path / "vault" / "notes").mkdir(parents=True)  # a real vault: a bad path is refused earlier
    monkeypatch.setattr(capture_mod, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(capture_mod, "DONE", tmp_path / "done.txt")
    monkeypatch.setattr(capture_mod, "REPASS", tmp_path / "repass.json")
    monkeypatch.setattr(capture_mod, "heal_index", lambda: "")
    monkeypatch.setattr(capture_mod, "endpoint_up", lambda: (True, "http://127.0.0.1:9/v1"))
    monkeypatch.setattr(capture_mod, "candidates", lambda *a, **k: [
        {"profile": "cipher", "session": "s1", "last": "now", "prose": 5000,
         "title": "a session", "regrew_from": 0},
    ])

    lock = (state / "lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = capture_mod.main(["--vault", str(tmp_path / "vault")])
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    out = capsys.readouterr().out
    assert "another sigil-capture run holds the lock" in out, out
    assert "quipu-capture" not in out
    assert rc == 0


# ---------------------------------------------------------------------------
# 3 — the benchmarks read the vault's index, and fail loudly when there is none
# ---------------------------------------------------------------------------


def test_the_benchmark_helper_reads_the_new_meta_dir(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()

    assert bench_vault.index_db(root) == root / ".sigil" / "index.db"


def test_the_benchmark_helper_finds_a_legacy_vault(tmp_path):
    root = tmp_path / "vault"
    (root / LEGACY_META_DIR).mkdir(parents=True)

    assert bench_vault.index_db(root) == root / LEGACY_META_DIR / "index.db"


def test_a_missing_index_is_a_loud_failure_not_a_zero_score(tmp_path):
    """A benchmark over an index it cannot find must say so — zeros would be a published lie."""
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(SystemExit) as exc:
        bench_vault.require_index(root)

    assert "no index at" in str(exc.value)


def test_no_benchmark_hardcodes_the_pre_rename_meta_dir():
    for name in ("run.py", "sweep_focus.py", "sweep_hybrid.py",
                 "consolidation_probe.py", "live_queries.py"):
        src = (ROOT / "benchmarks" / name).read_text(encoding="utf-8")
        assert f'"{LEGACY_META_DIR}"' not in src, f"benchmarks/{name} still hardcodes the old meta dir"
        assert "index_db" in src or "require_index" in src, \
            f"benchmarks/{name} does not resolve the index through the vault helper"


# ---------------------------------------------------------------------------
# 4 — the provider carries the name it was registered under
# ---------------------------------------------------------------------------


class _Ctx:
    """The shape Hermes hands a provider: it knows the name it was loaded by."""

    def __init__(self, name: str = ""):
        self.name = name
        self.providers: list = []

    def register_memory_provider(self, provider) -> None:
        self.providers.append(provider)


def test_the_provider_reports_the_name_it_was_registered_under():
    ctx = _Ctx("sigil")

    provider_mod.register(ctx)

    provider = ctx.providers[0]
    assert provider.name == "sigil", "a provider registered as sigil must not call itself quipu"
    assert sorted(s["name"] for s in provider.get_tool_schemas()) == ["sigil_search", "sigil_write"]


def test_a_context_without_a_name_still_reports_sigil():
    """There is one name to select since 0.10.0, so a nameless context cannot fall back to the old one."""
    ctx = _Ctx("")

    provider_mod.register(ctx)

    assert ctx.providers[0].name == "sigil"


def test_an_old_tool_name_is_refused_not_silently_answered():
    """A session holding a pre-0.10.0 `quipu_*` schema is stale; the error says which tools exist."""
    provider = provider_mod.SigilMemoryProvider(name="sigil")

    stale = provider.handle_tool_call("quipu_search", {"query": "wheel filename"})
    unknown = provider.handle_tool_call("sigil_explode", {})

    assert "not 'quipu_search'" in stale, stale
    assert "not 'sigil_explode'" in unknown, unknown


def test_the_provider_threads_and_logs_are_named_for_sigil():
    src = (ROOT / "sigil" / "hermes_provider.py").read_text(encoding="utf-8")

    assert '"quipu-sync"' not in src
    assert '"quipu-prefetch"' not in src
    assert 'logger.warning("quipu' not in src
    assert 'logger.debug("quipu' not in src


# ---------------------------------------------------------------------------
# 5 — the messages a user reads name what exists
# ---------------------------------------------------------------------------


def test_the_brief_names_the_current_commands():
    empty = brief_mod.build([])
    assert "Promote one with `sigil promote`" in empty

    one = brief_mod.build([Entry(kind="decision", state="promoted", summary="a decision",
                                 evidence="measured", agent="cipher", project="sigil")])
    assert "Generated by `sigil brief`" in one


def test_the_mcp_log_prefix_names_the_current_product(capsys):
    mcp_mod._log("hello")

    assert capsys.readouterr().err.strip() == "sigil-mcp: hello"


def test_the_edge_repair_hint_names_the_current_command(cfg):
    with pytest.raises(ValueError) as exc:
        graph_mod.add_edge(cfg, "a", "part_of", "b", note_id="deadbeef")

    assert "sigil repair --edges" in str(exc.value)


def test_the_audit_hint_names_the_current_command():
    src = (ROOT / "sigil" / "cli.py").read_text(encoding="utf-8")

    assert "rerun `sigil reindex`" in src
    assert "rerun `quipu reindex`" not in src


def test_the_embed_hints_name_the_current_command_and_path():
    src = (ROOT / "sigil" / "embed.py").read_text(encoding="utf-8")

    assert "run `sigil embed`" in src
    assert ".sigil/config.toml" in src
    assert ".quipu/config.toml" not in src


def test_the_timeout_hint_names_the_current_config_path():
    src = (ROOT / "sigil" / "promote.py").read_text(encoding="utf-8")

    assert ".sigil/config.toml" in src
    assert ".quipu/config.toml" not in src


def test_the_mcp_server_banner_names_the_current_product():
    src = (ROOT / "sigil" / "mcp.py").read_text(encoding="utf-8")

    assert "sigil-mcp:" in src
    assert "(sigil " in src
    assert "quipu-mcp:" not in src
