"""The packaging contract Hermes depends on (#10).

Hermes resolves a memory provider's *directory* from its entry point without importing it, and only
a package ``__init__`` yields one — so the entry point must name ``sigil``, not
``sigil.hermes_provider``. Pointing it at the bare module made a correctly installed provider report
"configured but not installed and not in the plugin catalog", which reads as a bad install and is
actually a bad entry point.

These assertions are cheap and they fail on the exact mistake that cost an hour: the discovery layer
inspects the module origin, so a module origin is indistinguishable from an absent provider.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

try:  # tomllib is 3.11+; CI still runs 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - interpreter-dependent
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]


def _entry_point() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    if tomllib is not None:
        group = tomllib.loads(text)["project"]["entry-points"]["hermes_agent.memory_providers"]
    else:  # 3.10: the CLI's _mini_toml does not cover quoted table names, so read the block
        block = re.search(
            r'^\[project\.entry-points\."hermes_agent\.memory_providers"\]\n((?:[^\[\n].*\n)*)',
            text,
            re.M,
        )
        assert block, "the provider entry-point table is missing from pyproject.toml"
        group = dict(re.findall(r'^([\w.\-]+)\s*=\s*"([^"]+)"', block.group(1), re.M))
    # one name since 0.10.0: the alias retired and took its entry point with it (#135)
    values = group if isinstance(group, dict) else {p.name: p for p in group}
    assert sorted(values) == ["sigil"], \
        "the provider is selected by name: memory.provider: sigil"
    return values["sigil"]


def test_the_entry_point_names_a_package_module():
    value = _entry_point()
    module, _, attr = value.partition(":")
    assert attr == "register", "Hermes calls this attribute with its registration context"
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None
    assert spec.origin.endswith("__init__.py"), (
        f"entry point {value!r} resolves to {spec.origin!r}; Hermes needs a package __init__.py to "
        "find the provider's directory without importing it"
    )


def test_the_package_exposes_register():
    import sigil

    assert callable(sigil.register)


def test_register_registers_one_provider():
    import sigil

    class Ctx:
        def __init__(self) -> None:
            self.providers: list[object] = []

        def register_memory_provider(self, provider: object) -> None:
            self.providers.append(provider)

    ctx = Ctx()
    sigil.register(ctx)
    assert len(ctx.providers) == 1
    assert ctx.providers[0].name == "sigil"
