# Contributing

Small project, strong opinions, short list of rules.

## Setup

```bash
uv sync --extra dev     # or: python -m venv .venv && .venv/bin/pip install -e ".[dev]"
make test
make lint
```

## Git workflow

`main` is protected: no direct pushes, squash-merge only, linear history, force-push and deletion
blocked. Every change is an issue → a branch → a PR → a review → a squash.

```
main
 └── feat/<issue>-<slug>     one branch per issue, short-lived
      └── PR → review checklist → squash merge → branch deleted
```

- **Branch prefix** matches the commit type: `feat/`, `fix/`, `docs/`, `test/`, `ci/`, `chore/`,
  `refactor/`, `perf/`.
- **Commit messages** are conventional commits; the body explains *why* and names the failure it
  prevents.
- **One task in flight.** One branch with code changes open at a time. Finish → verify → merge →
  then open the next. Parallel code streams on one codebase is how a project dissolves.
- **Exclusive path ownership per task**, declared in the issue. Source trees, tests, and docs have
  different owners and never overlap on the same branch.
- **The gate is quoted, not described.** A PR says `34 passed` with the command, not "tests pass".

### Who does what

| role | owns | never does |
|---|---|---|
| **orchestrator** (glitch) | verification, commits, merges, releases, anything live | hand-writes feature code while a builder is idle |
| **builder** (cipher) | `sigil/**` on its branch | commit, push, merge, touch `tests/` or `docs/` |
| **QA** (vector) | `tests/**`, gate scripts, adversarial review of the builder's diff | verify its own code, or the builder verify its own tests |
| **writer** (glyph) | `docs/**`, README, PLAN, release notes | change behaviour, or edit code |
| **research** (whoever is free) | `benchmarks/**`, external surveys, measurement reports | land code without a review |

The profiles that exist on this box are `glitch`, `cipher`, `vector`, `glyph` and `default`;
`cipher`, `vector` and `glyph` are peer-dispatchable through `self/<profile>`. `default` is the user's
own agent and is not dispatchable through the peer key, so research/benchmark work goes to whichever
worker is free rather than to a dedicated research profile. (An earlier version of this table named
`grok` as the research owner; there is no such profile — it was a name from the old fabric corpus.)

A worker never commits and never verifies its own work: the builder writes code, QA's tests are the
gate, the orchestrator runs the gate and commits. That rule exists because a worker's "tests pass" is
a self-report — it has already been wrong here once.

## Rules

1. **Markdown is the source of truth.** Anything that can only be reconstructed from SQLite is a bug.
2. **A test or it did not happen.** Behaviour changes need a test that fails before the change. The
   pipeline is testable without a model — use `FakeProvider`.
3. **No new runtime dependencies** without an argument in the PR that names what breaks without it.
   `pyproject.toml` currently has an empty `dependencies` list and that is a feature.
4. **No silent failure.** Hooks, migrations and background passes must exit non-zero and say what
   broke. A memory system that fails quietly is worse than one that fails loudly.
5. **Never invent a benchmark number.** If a claim has not been measured on a named dataset with a
   committed result file, it does not go in the README.

## Commit messages

`type: what changed` — `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `perf:`, `chore:`.
The body explains *why*, and names the failure it prevents when that is the reason.

## Adding a predicate

Predicates are a closed vocabulary on purpose. Add one in `.sigil/config.toml` for your vault, and to
`DEFAULT_PREDICATES` in `sigil/__init__.py` only if it is generally useful — with a line in the PR
saying which existing predicate it cannot be expressed as.
