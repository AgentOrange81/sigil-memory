# Rename quipu → Sigil — migration plan

> **For Hermes:** work this top to bottom. Phases 1–2 are reversible; phases 3–5 touch live state and
> each one ends with a verification that a skeptic can re-run.

**Goal:** ship the same memory engine under the name **Sigil** — `sigil` on the CLI, `sigil-memory` on
PyPI, `~/sigil-vault` on disk — without a single broken reference, and without rewriting the 1,027
notes that talk about the old name.

**Architecture:** alias-first. `sigil` becomes the real package, and `quipu` survives one release as a
thin shim (console script + importable module) so the MCP servers, the systemd timer, the admin scripts
and every doc that mentions it keep working while the pieces move one at a time. Nothing is renamed by
`sed` across the tree; each surface moves in its own commit with its own check.

**Tech stack:** Python 3.10–3.13, pytest, systemd user units, Hermes MCP config, GitHub releases.

---

## Measured surface (2026-11-11, commands included)

| surface | count | how it was measured |
|---|---|---|
| files in the repo that mention `quipu` | **93** | `grep -rl --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ -i quipu` |
| — of those, `tests/` | 41 | same, per directory |
| — `benchmarks/` | 15 | |
| — `docs/` | 6, `.github/` 3, `importers/` 2, one each in 20 modules + README/PLAN/Makefile/CONTRIBUTING | |
| Hermes profile configs naming the MCP server | **4** (glitch, cipher, glyph, vector) | `grep -rli quipu ~/.hermes/profiles/*/config.yaml` |
| memory-provider reference | 1 line, `provider: quipu` | `~/.hermes/profiles/glitch/config.yaml:418` |
| systemd units | **2** (`quipu-capture.service`, `.timer`) | `ls ~/.config/systemd/user | grep -i quipu` |
| installed binaries | 2 (`~/.local/bin/quipu`, `quipu-capture` — the latter is a 10-line shim since #131) | `ls -la ~/.local/bin` |
| live state dir | 1 (`~/.local/state/quipu-capture/`) | |
| admin scripts | 3 (`quipu-evidence-backfill.py`, `quipu-rollout-check.py`, `restart-gateway-for-quipu.sh`) | `ls ~/admin/*quipu*` |
| vault | `~/quipu-vault` — **2,072 notes**, internals under `.quipu/` | `quipu audit` |
| notes whose *prose* says quipu | **1,027** | `grep -rl -i quipu ~/quipu-vault/notes | wc -l` |
| skills | 1 skill dir (`software-development/quipu/`, SKILL.md + 5 references + `alpha_probe.py`) + mentions in 6 others | `grep -rli quipu ~/.hermes/profiles/glitch/skills` |
| PyPI | `sigil` **taken**, `sigil-memory` **free** | `curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/<name>/json` |
| domains | `sigil.io` free, `sigil.fun` free | `getent hosts` |
| binary collision | none (`/usr/bin/sigil`, apt: absent) | `ls /usr/bin/sigil`, `apt-cache policy sigil` |

**Decision, recorded:** the name is Sigil, short form `sigil`, distribution `sigil-memory`, vault
`~/sigil-vault`. Rejected: skein (it is a hash function), palimpsest/anamnesis (unusable as commands),
thaum (lore before meaning), augur (runner-up — names the oracle rather than the mark), grimoire /
hex / rune / codex / familiar (saturated).

**Version:** unchanged at **0.8.0**. A rename is not a shape change, so the next tag is `v0.8.1` cut
after phase 6. (A reset to 0.2.1 was considered and dropped.)

---

## Phase 1 — the alias scaffold (reversible)

### Task 1: `sigil` package that re-exports `quipu`

**Files:**
- Create: `sigil/__init__.py`
- Test: `tests/test_sigil_alias.py`

**Step 1:** write the failing test.

```python
def test_the_new_name_imports_the_same_engine():
    import quipu, sigil
    assert sigil.MEMORY_ENGINE is quipu.MEMORY_ENGINE   # whatever the top-level handle actually is
    assert sigil.__version__ == quipu.__version__
```

**Step 2:** `pytest tests/test_sigil_alias.py -q` → FAIL (`ModuleNotFoundError: sigil`).

**Step 3:** implement `sigil/__init__.py` as `from quipu import *  # noqa: F401,F403` plus
`__version__ = quipu.__version__`, and add `sigil` to `[tool.setuptools.packages.find]` if it lists
packages explicitly.

**Step 4:** `pytest tests/test_sigil_alias.py -q` → PASS.

**Step 5:** commit `feat: sigil imports quipu while the rename is in flight`.

> Why this order: on day one `import sigil` works and nothing else has moved, so every later task can
> be reverted independently.

### Task 2: `sigil` console script beside `quipu`

**Files:** `pyproject.toml` (`[project.scripts]`), `tests/test_release_metadata.py`

**Step 1:** failing test — both entry points resolve to the same callable.

```python
def test_both_console_scripts_point_at_one_main():
    text = PYPROJECT
    assert 'sigil = "quipu.cli:main"' in text
    assert 'quipu = "quipu.cli:main"' in text
```

**Step 2:** run it → FAIL. **Step 3:** add the `sigil` line. **Step 4:** `uv pip install -e .` then
`sigil --version` → `quipu 0.8.0` (the banner still says quipu; that is phase 2's job). **Step 5:** commit.

---

## Phase 2 — the code moves (reversible, one commit each)

### Task 3: `git mv quipu sigil`, with the old name left as a shim

**Files:** `quipu/` → `sigil/`, create `quipu/__init__.py` shim, `pyproject.toml`.

**Step 1:** `git mv quipu sigil` then `git status` to confirm it reads as renames, not add/delete.

**Step 2:** rewrite intra-package imports: `grep -rl 'from quipu\|import quipu' sigil/ | xargs sed -i 's/\bfrom quipu\b/from sigil/g; s/\bimport quipu\b/import sigil/g'`
— then **read the diff**, because a blind sed also rewrites prose and test names.

**Step 3:** create the shim so old importers keep working:

```python
"""Deprecated: the engine is `sigil` now. Kept for one release so existing imports keep resolving."""
import warnings
from sigil import *  # noqa: F401,F403
warnings.warn("import sigil instead of quipu", DeprecationWarning, stacklevel=2)
```

**Step 4:** `pytest -q` → **554 passed** (the whole suite must pass before any surface outside the repo
moves; a failure here means the sed touched something it should not have).

**Step 5:** commit `refactor: the package is sigil, with quipu as a deprecation shim`.

### Task 4: the CLI banner and `--help` say Sigil

**Files:** `sigil/cli.py` (`--version`), `sigil/mcp.py` (`serverInfo.name`), `tests/test_cli.py`.

**Step 1:** failing test — `--version` prints `sigil 0.8.0`, and the MCP server info says `sigil`.
**Step 2:** implement. **Step 3:** `sigil --version` → `sigil 0.8.0`; `quipu --version` → the same string
with a one-line deprecation note on stderr. **Step 4:** commit.

### Task 5: the entry point and the provider name

**Files:** `pyproject.toml`, `tests/test_provider_entry_point.py`.

`hermes_agent.memory_providers` currently maps `quipu = "quipu:register"`. Add `sigil = "sigil:register"`
**beside** it for one release — removing the old key is what would make an existing config fail to
resolve at gateway start.

**Verification:** `python -c "import importlib.metadata as m; print([e for e in m.entry_points(group='hermes_agent.memory_providers')])"`
shows both.

---

## Phase 3 — the vault moves (the only risky step)

### Task 6: copy, verify, then swap — never `mv` blind

**Files:** `~/quipu-vault` → `~/sigil-vault`; `.quipu/` internals become `.sigil/`; `sigil/config.py`.

**Step 1:** stop the writers so the copy is consistent:

```bash
systemctl --user stop quipu-capture.timer quipu-capture.service
```

**Step 2:** copy, do not move:

```bash
cp -a ~/quipu-vault ~/sigil-vault-copy && du -sh ~/quipu-vault ~/sigil-vault-copy
```

**Step 3:** rename the internal dir **in the copy only**:

```bash
mv ~/sigil-vault-copy/.quipu ~/sigil-vault-copy/.sigil
```

**Step 4:** `config.load` accepts either name — the fallback is what makes a half-migrated vault safe
to open. Test it before relying on it:

```python
def test_a_vault_that_still_has_the_old_internal_dir_opens(tmp_path):
    cfg = load(tmp_path / "v", create=True)
    (cfg.root / ".quipu").rename(cfg.root / ".sigil")   # simulate the old shape
    assert load(cfg.root).db_path.name == "index.db"
```

**Step 5:** verify the copy answers its own history **before** the original is touched:

```bash
sigil --vault ~/sigil-vault-copy reindex      # expect: indexed 2072 entries, rebuilt 1008 edges (919 live)
sigil --vault ~/sigil-vault-copy audit        # expect: 0 problems
sigil --vault ~/sigil-vault-copy stats        # expect: 1515 classified, same promoted count
```

Numbers to match, measured on the live vault on 2026-11-11: **2,072 entries, 919 live edges (1,008
rebuilt — the difference is the closed windows), 0 audit problems, 1,515 with `evidence_kind`.** A mismatch here means the copy is not equivalent — stop, do not
swap. **Do not "fix" a mismatch by eye**; find which step lost data.

**Step 6:** swap, keeping the old vault for a week:

```bash
mv ~/quipu-vault ~/quipu-vault.retired-2026-11-11 && mv ~/sigil-vault-copy ~/sigil-vault
```

**Step 7:** re-run step 5's three commands against `~/sigil-vault`, then commit the `config.py` change.

### Task 7: the capture ledger keeps its history

**Files:** `sigil/capture.py` (`DONE`, `REPASS` defaults).

The ledger at `~/.local/state/quipu-capture/done.txt` holds `prose=` sizes for sessions already read.
Copy it into the new state dir **before** the first tick under the new name, or every session captured
in the last fortnight becomes a candidate again — the exact stampede the `prose=` field prevents.

```bash
mkdir -p ~/.local/state/sigil-capture && cp -a ~/.local/state/quipu-capture/. ~/.local/state/sigil-capture/
sigil-capture --list    # expect: nothing to do  (a stampede means the ledger did not come across)
```

---

## Phase 4 — live surfaces (each needs the operator's go)

### Task 8: the four profile configs

**Files:** `~/.hermes/profiles/{glitch,cipher,glyph,vector}/config.yaml`

Per profile, two edits: the MCP server key `quipu:` → `sigil:` (with `command: /home/dirk/.local/bin/sigil`,
same vault path), and `provider: quipu` → `provider: sigil` at line 418 of glitch's config.

**Back up first:** `cp config.yaml config.yaml.pre-sigil-$(date +%F)`.

**Verification after the gateway restart (operator):** the tool namespace becomes `mcp__sigil__*` —
`sigil search "wraith"` through the MCP tool returns rows, and a `write` leaves the index's
`evidence_kind` intact (that regression is what #128 fixed; re-check it under the new namespace).

### Task 9: systemd units and the shims

```bash
systemctl --user disable --now quipu-capture.timer
sed -i 's/quipu/sigil/g' ~/.config/systemd/user/quipu-capture.service ~/.config/systemd/user/quipu-capture.timer
mv ~/.config/systemd/user/quipu-capture.service ~/.config/systemd/user/sigil-capture.service
mv ~/.config/systemd/user/quipu-capture.timer  ~/.config/systemd/user/sigil-capture.timer
systemctl --user daemon-reload && systemctl --user enable --now sigil-capture.timer
```

Then replace the two `~/.local/bin` files: `quipu` and `quipu-capture` become shims that exec the
`sigil` equivalents, so anything still calling them (this plan, old notes, my own habits) keeps working.

**Verification:** `systemctl --user start sigil-capture.service` → `Result=success`, exit 0, and
`journalctl --user -u sigil-capture.service -n 3` shows either `nothing to do` or a real pass.

### Task 10: admin scripts and skills

- `git mv`-equivalent for `~/admin/quipu-evidence-backfill.py` → `sigil-evidence-backfill.py`,
  `quipu-rollout-check.py` → `sigil-rollout-check.py`; rewrite the internal paths, not just the names.
- `software-development/quipu/` → `software-development/sigil/` via `skill_manage` delete+create is
  **wrong** (it loses history); instead move the directory and patch the `name:`/`description:` in
  SKILL.md, then fix the six other skills that mention it.
- Update `memory-vault` and `glitch-host-environment` references to the new vault path.

**Verification:** `grep -rli quipu ~/.hermes/profiles/glitch/skills` returns only the historical
mentions inside `references/` that describe quipu's own history — those are records, not references.

---

## Phase 5 — the repo identity

### Task 11: repository, distribution, docs

- GitHub repo rename `AgentOrange81/quipu` → `AgentOrange81/sigil` (GitHub keeps the redirect);
  `git remote set-url origin` afterwards.
- `pyproject.toml`: `name = "sigil-memory"`, `[project.scripts] sigil = "sigil.cli:main"`,
  `quipu` kept as the alias.
- README, CONTRIBUTING, PLAN, Makefile targets, `.github/workflows/*` (`quipu` → `sigil`).
- `~/Projects/quipu` → `~/Projects/sigil` (**after** the venv is rebuilt; an absolute path inside
  `.venv/bin/*` scripts still points at the old directory and fails silently — the `product-naming`
  skill records this trap).

### Task 12: the corpus decision — do not rewrite history

1,027 notes mention `quipu` in prose and many carry `project: quipu`. **Leave them.** They are records
of what was true then, and rewriting 1,000 historical entries to satisfy a rename is how a memory vault
stops being trustworthy. New entries carry `project: sigil`; the hub entry for the project gets one
line saying the rename happened on 2026-11-11 with a `supersedes` pointer.

---

## Phase 6 — release

### Task 13: tag and verify

```bash
git tag -a v0.8.1 -m "sigil 0.8.1 — the same engine under its own name"
git push origin v0.8.1
```

**Verification, from the published artifact:**

```bash
pip download --no-deps -d /tmp/w sigil-memory==0.8.1     # from PyPI, not GitHub, since it is free
python -m venv /tmp/v081 && /tmp/v081/bin/pip install /tmp/w/*.whl
/tmp/v081/bin/sigil --version        # sigil 0.8.1
/tmp/v081/bin/quipu --version        # sigil 0.8.1 (deprecated alias, same engine)
```

First PyPI publication ever for this project — so the publish step needs a token and, if it does not
exist yet, a TestPyPI dry run first.

---

## Acceptance

- [ ] `grep -rli quipu ~/Projects/sigil --exclude-dir=.git --exclude-dir=.venv` → only the shim, the
      deprecated entry points, and CHANGELOG history
- [ ] `sigil --vault ~/sigil-vault audit` → 0 problems, 2,072 entries
- [ ] `sigil-capture --list` → `nothing to do` (ledger carried over)
- [ ] MCP `mcp__sigil__search` answers and `mcp__sigil__write` leaves `evidence_kind` intact
- [ ] `pip install sigil-memory` works from PyPI
- [ ] `quipu` still runs, with a deprecation warning, for one release
