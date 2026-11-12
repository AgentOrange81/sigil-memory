"""Acceptance gate for the configurable LLM read timeout (cipher's llm-timeout branch).

Black-box contract tests, written against the CONTRACT — `Config.llm_timeout` (default
`DEFAULT_LLM_TIMEOUT`, 600 s since 0.5.2) saved
as `timeout = N` inside the `[llm]` block of `.sigil/config.toml`, `clil._provider()` wiring it
into `HttpProvider`, `--endpoint-timeout` on `reflect` and `hook` overriding it per command, and
a hard `ValueError` naming `llm.timeout` for any value `<= 0` or non-integer — never against an
implementation. On main (c476a61) the field does not exist: these tests fail there on purpose and
are the gate cipher's PR must satisfy.

Deterministic: tmp vaults only, no network, no sleeps. Nothing here calls the LLM.
"""

import argparse

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10: fall back to the parser load() itself uses
    tomllib = None

from sigil import config as config_mod
from sigil.cli import _provider, build_parser

DEAD_ENDPOINT = "http://127.0.0.1:9/v1"  # never contacted: _provider only constructs


def test_config_round_trip_of_llm_timeout(tmp_path):
    """llm_timeout=240 saved becomes `timeout = 240` inside [llm], and loads back as 240."""
    vault = tmp_path / "vault"
    cfg = config_mod.load(vault, create=True)
    cfg.llm_timeout = 240
    cfg.save()

    text = (vault / ".sigil" / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text) if tomllib else config_mod._mini_toml(text)
    assert data["llm"]["timeout"] == 240, f"the raw [llm] block must carry the timeout:\n{text}"

    again = config_mod.load(vault)
    assert again.llm_timeout == 240, "the saved timeout must load back"


def test_a_fresh_vault_takes_the_default_budget(tmp_path):
    """The default is a measured number, not a taste: see DEFAULT_LLM_TIMEOUT in quipu/config.py.

    A 12-line slice of a real Hermes session is 68 KB; at 120 s the on-box 35B tier timed out on a 178 KB
    chunk. The default must cover a real chunk or the pass fails on exactly what it is for.
    """
    cfg = config_mod.load(tmp_path / "vault", create=True)
    assert cfg.llm_timeout == config_mod.DEFAULT_LLM_TIMEOUT
    assert cfg.llm_timeout >= 600


def test_the_client_default_matches_the_config_default():
    """Two defaults for one number is how a provider call silently keeps the old budget."""
    from sigil.promote import HttpProvider

    assert HttpProvider("http://127.0.0.1:9/v1", "m").timeout == config_mod.DEFAULT_LLM_TIMEOUT


def test_a_timeout_names_the_knobs(monkeypatch):
    """The old message named the endpoint and stopped there, so a user had nothing to turn."""
    import urllib.request

    from sigil.promote import HttpProvider

    def boom(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as caught:
        HttpProvider("http://127.0.0.1:8081/v1", "m", timeout=600).complete("hello")
    message = str(caught.value)
    assert "no response within 600s" in message
    assert "--endpoint-timeout" in message and "[llm] timeout" in message and "--max-chars" in message


def test_a_connection_error_is_not_dressed_up_as_a_timeout(monkeypatch):
    import urllib.error
    import urllib.request

    from sigil.promote import HttpProvider

    def boom(*_a, **_k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as caught:
        HttpProvider("http://127.0.0.1:9/v1", "m").complete("hello")
    assert "--max-chars" not in str(caught.value), "chunk-size advice belongs on a timeout only"


def test_the_config_value_reaches_the_provider(tmp_path):
    """_provider must construct HttpProvider with the config's timeout. Constructor only."""
    vault = tmp_path / "vault"
    cfg = config_mod.load(vault, create=True)
    cfg.llm_timeout = 240
    cfg.save()
    cfg = config_mod.load(vault)

    provider = _provider(argparse.Namespace(fake=None, model="", endpoint_timeout=None), cfg)
    assert provider.timeout == 240, (
        f"the provider must get the config's timeout, got {provider.timeout}")


def test_the_cli_flag_beats_the_config(tmp_path):
    vault = tmp_path / "vault"
    cfg = config_mod.load(vault, create=True)  # fresh vault: default 120 in effect
    provider = _provider(argparse.Namespace(fake=None, model="", endpoint_timeout=30), cfg)
    assert provider.timeout == 30, (
        f"--endpoint-timeout must override the config value, got {provider.timeout}")


@pytest.mark.parametrize("bad", [0, -5, '"thirty"', 1.5],
                         ids=["zero", "negative", "string", "float"])
def test_a_value_that_cannot_be_a_timeout_is_refused(tmp_path, bad):
    """`<= 0` and non-integers in the config raise ValueError on load, naming `llm.timeout`."""
    vault = tmp_path / "vault"
    cfg = config_mod.load(vault, create=True)
    cfg.save()
    toml_path = vault / ".sigil" / "config.toml"
    text = toml_path.read_text(encoding="utf-8")
    assert "[llm]" in text
    toml_path.write_text(text.replace("[llm]\n", f"[llm]\ntimeout = {bad}\n"), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        config_mod.load(vault, create=False)
    assert "llm.timeout" in str(excinfo.value), (
        f"the message must name the broken setting: {excinfo.value}")


def test_hook_carries_both_timeout_knobs(tmp_path):
    """hook's --timeout (the reflection-abort knob, dest `timeout`) and --endpoint-timeout (the
    LLM read timeout, dest `endpoint_timeout`) are different knobs and must coexist. Shape:
    the CLI exposes build_parser(), so the subparser is driven through public parse_args."""
    text_path = tmp_path / "vault"

    hook = build_parser().parse_args(["--vault", str(text_path), "hook", "--timeout", "9"])
    assert hook.timeout == 9, "hook keeps its own --timeout knob (dest: timeout)"

    try:
        hooked = build_parser().parse_args(
            ["--vault", str(text_path), "hook", "--endpoint-timeout", "30"])
    except SystemExit:
        pytest.fail("the hook subparser does not accept --endpoint-timeout (dest endpoint_timeout)")
    assert hooked.endpoint_timeout == 30, \
        "the flag must land on dest endpoint_timeout, not be swallowed by a prefix match"

    both = build_parser().parse_args(
        ["--vault", str(text_path), "hook", "--timeout", "9", "--endpoint-timeout", "30"])
    assert both.timeout == 9 and both.endpoint_timeout == 30, \
        "the two knobs must parse in one command without merging"

    # item 3 also names the flag on reflect: same override, same dest
    reflection = build_parser().parse_args(
        ["--vault", str(text_path), "reflect", "--endpoint-timeout", "45"])
    assert reflection.endpoint_timeout == 45