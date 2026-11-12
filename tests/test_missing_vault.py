"""#141: a path that is not a vault is refused, not created and answered from.

The incident: a vault was moved during the rename, `sigil_search` answered `[]`, and the path the caller
was pointed at had been *created* by that read — an empty vault holding an empty index. The answer was
indistinguishable from "there is no memory about this". Every test here asserts both halves of the fix:
the loud failure AND that nothing appeared on disk.
"""
import json
import subprocess
import sys

import pytest

from sigil import config as config_mod
from sigil import mcp as mcp_mod
from sigil.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def read_paths():
    """The commands whose job is to answer FROM a vault: (name, extra argv) per CLI read surface."""
    return [("search", ["quipu"]), ("timeline", ["quipu"]), ("neighbors", ["quipu"]),
            ("stats", []), ("audit", []), ("brief", [])]


# -- the guard itself -------------------------------------------------------------------------------

def test_load_refuses_a_path_that_does_not_exist(tmp_path):
    missing = tmp_path / "no-such-vault"
    with pytest.raises(config_mod.VaultNotFound) as exc:
        config_mod.load(missing)
    assert str(missing) in str(exc.value)
    assert not missing.exists(), "a refused read must not create the vault it was pointed at"


def test_load_refuses_an_existing_directory_that_is_not_a_vault(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(config_mod.VaultNotFound) as exc:
        config_mod.load(empty)
    assert str(empty) in str(exc.value)
    assert sorted(p.name for p in empty.iterdir()) == [], "nothing may be written into a refused path"


def test_load_refuses_a_file(tmp_path):
    file_path = tmp_path / "notes.md"
    file_path.write_text("not a vault\n")
    with pytest.raises(config_mod.VaultNotFound):
        config_mod.load(file_path)


def test_the_message_says_how_to_fix_it(tmp_path):
    with pytest.raises(config_mod.VaultNotFound) as exc:
        config_mod.load(tmp_path / "typo")
    message = str(exc.value)
    assert "--vault" in message and "sigil init" in message


# -- the shapes that ARE vaults, so the guard is not a false positive --------------------------------

def test_a_vault_with_only_notes_is_still_a_vault(tmp_path):
    (tmp_path / "notes").mkdir()
    cfg = config_mod.load(tmp_path)
    assert cfg.root == tmp_path.resolve()


def test_a_pre_rename_vault_with_only_dot_quipu_is_still_a_vault(tmp_path):
    (tmp_path / ".quipu").mkdir()
    cfg = config_mod.load(tmp_path)
    assert cfg.meta_dir.name == ".quipu"


def test_create_still_creates(tmp_path):
    root = tmp_path / "fresh"
    cfg = config_mod.load(root, create=True)
    assert (root / "notes").is_dir() and (root / ".sigil" / "config.toml").exists()
    assert config_mod.load(root).root == cfg.root


# -- the CLI -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,extra", read_paths(), ids=[c for c, _ in read_paths()])
def test_every_read_command_exits_two_and_creates_nothing(cfg, capsys, tmp_path, command, extra):
    missing = tmp_path / "moved-away-vault"
    code, out, err = run(capsys, "--vault", str(missing), command, *extra)
    assert code == 2, f"{command} answered from a vault it invented"
    assert str(missing) in err, "the failure must name the path it tried"
    assert not missing.exists()


def test_init_is_the_way_in(cfg, capsys, tmp_path):
    root = tmp_path / "brand-new"
    code, out, err = run(capsys, "--vault", str(root), "init")
    assert code == 0 and "initialised" in out
    assert (root / "notes").is_dir() and (root / ".sigil" / "index.db").exists()


def test_write_refuses_a_missing_vault_instead_of_minting_one(cfg, capsys, tmp_path):
    missing = tmp_path / "typo-vault"
    code, out, err = run(capsys, "--vault", str(missing), "write", "--summary", "s", "--evidence", "e")
    assert code == 2
    assert str(missing) in err
    assert not missing.exists()


def test_the_old_behaviour_is_really_gone_a_read_leaves_no_trace(cfg, capsys, tmp_path):
    """The incident reproduced end to end: read a moved vault, then look for what the read left behind."""
    root = tmp_path / "vault-that-moved"
    code, _out, _err = run(capsys, "--vault", str(root), "search", "quipu")
    assert code == 2
    assert not root.exists(), "the read created a vault — exactly the #141 bug"


# -- the MCP server ----------------------------------------------------------------------------------

def test_mcp_serve_refuses_a_missing_vault_and_names_it(tmp_path, capsys):
    missing = tmp_path / "gone"
    assert mcp_mod.serve(missing) == 2
    err = capsys.readouterr().err
    assert "refusing to serve" in err and str(missing) in err
    assert not missing.exists()


def test_mcp_binary_exits_nonzero_before_reading_stdin(tmp_path):
    """The harness runs the console script, not serve() — the exit code is what it sees."""
    missing = tmp_path / "gone"
    proc = subprocess.run(
        [sys.executable, "-m", "sigil", "--vault", str(missing), "mcp"],
        input=b"", capture_output=True, timeout=60,
    )
    assert proc.returncode == 2
    assert str(missing).encode() in proc.stderr
    assert not missing.exists()


# -- the provider ------------------------------------------------------------------------------------

def test_provider_write_returns_the_error_instead_of_raising(cfg, tmp_path):
    from sigil.hermes_provider import SigilMemoryProvider  # noqa: PLC0415 - optional dep graph

    provider = SigilMemoryProvider.__new__(SigilMemoryProvider)  # bypass __init__: no live vault needed
    provider._vault = str(tmp_path / "gone")
    provider._session_id = "s"
    out = json.loads(provider._tool_write({"summary": "s", "evidence": "e"}))
    assert "error" in out and str(tmp_path / "gone") in out["error"]
    assert not (tmp_path / "gone").exists()


# -- the capture timer -------------------------------------------------------------------------------

def test_capture_refuses_a_missing_vault_and_creates_nothing(tmp_path, capsys, monkeypatch):
    """A timer pointed at a bad path must fail the unit, not report 'nothing to do' forever (#141)."""
    from sigil import capture as capture_mod  # noqa: PLC0415 - only the CLI entry point needs it

    monkeypatch.setattr(capture_mod, "VAULT", capture_mod.VAULT)  # main() rebinds the global
    missing = tmp_path / "not-there"
    assert capture_mod.main(["--vault", str(missing)]) == 1
    out = capsys.readouterr().out
    assert str(missing) in out and "Nothing was created" in out
    assert not missing.exists()
