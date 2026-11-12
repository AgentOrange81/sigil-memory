# Plan — remove the `quipu` alias at 0.10.0

Measured, not estimated. On branch `probe/alias-removal` off `923b0ca`, deleting the `quipu/` package
and running the suite gives:

```
25 failed, 20 errors, 525 passed
```

Every one of those 45 is a **test-side** use of the old name, not a product dependency. Classified:

| what | count | where | verdict at 0.10.0 |
|---|---|---|---|
| `subprocess.run([sys.executable, "-m", "quipu", ...])` fixtures | 31 | `test_edge_carrier`, `test_supersedes_graph`, `test_edge_specs`, `test_graph_replay`, `test_mcp`, `test_session_db` | swap to `-m sigil` — mechanical |
| alias-behaviour assertions | 14 in `test_name.py` | identity, warning, `-m quipu`, pyproject scripts/entry points | delete with the shim; keep the `sigil` half |
| an **entity named** `quipu` | 1 | `test_edge_carrier.py:74` (`edge quipu depends_on sqlite`) | keep — that is data, not the package |

## What dies

- `quipu/` (two files, 63 lines: the meta-path finder and `__main__.py`).
- Console scripts `quipu`, `quipu-capture`.
- Entry point `hermes_agent.memory_providers: quipu`.
- `import quipu`, `python -m quipu`, `quipu --version`.

## What must NOT die — this is data compatibility, not naming

A vault written before the rename must keep opening. These are permanent, and removing them would be a
data-loss bug wearing a tidy-up costume:

- `config.LEGACY_META_DIR = ".quipu"` and the fallback that reads it where it lies.
- `QUIPU_VAULT` in the provider's resolver, and the `[quipu]` config-table parse.
- `memory.quipu` as a config block name in the provider.
- `quipu` in the evidence command vocabulary — decide explicitly: keep it (cheap, and old evidence keeps
  classifying) or drop it (tidy, and any note evidencing `quipu audit` reclassifies to `claim`). Keep it;
  a classifier's vocabulary is about history, not branding.

## Sequence

1. Confirm the deprecation warning fires exactly once per process and names the replacement. A shim that
   warns on every import trains people to filter it out.
2. Delete `quipu/`, the two scripts, the one entry point.
3. Swap the 31 fixtures to `-m sigil` **in the same commit** — a green suite is the proof the alias had no
   other user.
4. Delete the 14 alias assertions in `test_name.py`; keep `test_the_wheel_declares_both_names` style checks
   inverted to assert the old names are *gone* (that is the regression test for a re-introduced alias).
5. CHANGELOG: a breaking-change section that says what to run instead, and what still works for old vaults.
6. Gate: `pytest` at 570 (all renamed, none lost), `ruff`, plus the wheel smoke in CI.

## One thing to check before cutting 0.10.0

`pip install quipu` on PyPI is Matt Good's package, so anyone following an old doc gets someone else's
code. If any published artifact or doc still tells a user to `pip install quipu`, that is worse than a
stale name — fix that before the alias dies.
