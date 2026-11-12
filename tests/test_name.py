"""The old name is retired at 0.10.0 — this file is the regression test for that.

Until 0.10.0 every surface here was pinned to work under BOTH names, because a rename that breaks an
import, a console script or the provider entry point breaks a profile's gateway at startup. The alias
had one release of grace and it is now spent, so these assertions run the other way: they fail if the
old name is quietly re-introduced, which would let a stale config keep pointing a working install at a
name nobody tests.

What is deliberately NOT retired — data compatibility, not naming — and therefore not asserted here:
`.quipu/` meta dirs (`test_legacy_vault_name`), a `[quipu]` config table (`test_config`), `QUIPU_VAULT`
and `~/quipu-vault` in the provider's resolver, `memory.quipu.*` settings, and `quipu` in the evidence
command vocabulary. A vault written before the rename must keep opening; deleting those would be a
data-loss bug wearing a tidy-up costume.
"""
from __future__ import annotations

import importlib.metadata as md
import importlib.util
import re
from pathlib import Path

import pytest

import sigil

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_old_package_name_is_gone():
    """`import quipu` must fail: a working import means the shim is back, or a stale wheel is installed."""
    assert importlib.util.find_spec("quipu") is None, (
        "the quipu alias is importable again — if this is not a re-introduced package, uninstall the "
        "stale copy (`pip list | grep -i quipu`) because it shadows the retired name"
    )


def test_the_old_module_path_is_gone():
    """`python -m quipu` is the other thing people hardcode; it must now be a plain ModuleNotFoundError."""
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "quipu", "--version"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode != 0, "`python -m quipu` still runs the CLI"
    assert "No module named" in out.stderr, out.stderr


def test_pyproject_declares_the_new_name_only():
    """One script per entry point, one provider entry point, and only the sigil package in the wheel."""
    assert re.search(r'^sigil = "sigil\.cli:main"$', PYPROJECT, re.M)
    assert re.search(r'^sigil-capture = "sigil\.capture:main"$', PYPROJECT, re.M)
    assert re.search(r'^sigil = "sigil:register"$', PYPROJECT, re.M)
    assert re.search(r'^quipu = ', PYPROJECT, re.M) is None, "a quipu script or entry point is back"
    assert re.search(r'^quipu-capture = ', PYPROJECT, re.M) is None
    assert re.search(r'^packages = \["sigil"\]$', PYPROJECT, re.M), (
        "the wheel must ship exactly one package: a second one is either the alias returning or a "
        "package nobody tests"
    )


def test_the_installed_metadata_has_no_old_name():
    """The entry points Hermes resolves from a real install, not the source tree."""
    try:
        providers = {p.name for p in md.entry_points(group="hermes_agent.memory_providers")}
    except Exception:  # pragma: no cover - an uninstalled source tree
        pytest.skip("no installed metadata")
    ours = {n for n in providers if n in {"sigil", "quipu"}}
    if not ours:
        pytest.skip("package not installed in this interpreter")
    assert ours == {"sigil"}, f"stale install still advertises {ours - {'sigil'}} — reinstall the package"


def test_the_version_banner_says_sigil(capsys):
    from sigil import cli

    with pytest.raises(SystemExit):
        cli.main(["--version"])

    assert capsys.readouterr().out.strip() == f"sigil {sigil.__version__}"


def test_the_mcp_server_calls_itself_sigil():
    """An agent reading `serverInfo.name` should see the product it is talking to."""
    from sigil import mcp

    source = mcp.__file__ and Path(mcp.__file__).read_text(encoding="utf-8")

    assert '"serverInfo": {"name": "sigil"' in source
    assert '"serverInfo": {"name": "quipu"' not in source


def test_a_vault_written_under_the_old_name_still_opens(tmp_path):
    """The point of removing the name is not to remove the data: `.quipu/` is read where it lies."""
    from sigil import config as config_mod

    (tmp_path / "notes").mkdir()
    (tmp_path / ".quipu").mkdir()
    (tmp_path / ".quipu" / "config.toml").write_text("[quipu]\npromote_threshold = 0.7\n", encoding="utf-8")

    cfg = config_mod.load(tmp_path)
    assert cfg.meta_dir.name == ".quipu"
    assert cfg.promote_threshold == 0.7
