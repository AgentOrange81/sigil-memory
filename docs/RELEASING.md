# Releasing sigil

A release is three things that must agree: the version string, the CHANGELOG section, and the tag.
CI builds the artifacts from the tag; everything below is what a person does before and after that.

## The version lives in three places

| file | what it holds | who reads it |
|---|---|---|
| `pyproject.toml` | `version = "0.10.2"` | the wheel and sdist filenames, and pip |
| `sigil/__init__.py` | `__version__ = "0.10.2"` | `sigil --version`, the MCP `serverInfo.version` |
| `sigil/plugin.yaml` | `version: 0.10.2` | `hermes plugins list`, per profile — the number agents believe |

All three must move together, in the same commit. A mismatch is not a build error: the wheel installs
and `sigil --version` prints a string the artifact does not carry, so the published version and
the reported version disagree with no signal that anything is wrong. The manifest is the one that
rots quietly — nothing in the build reads it, so it keeps advertising the previous release in every
profile until someone looks. `tests/test_release_metadata.py` fails on all three.

## CHANGELOG first

`## Unreleased` becomes the release heading, and the release heading goes at the top — newest
section first:

```markdown
## 0.10.2 — 2026-11-12
```

The heading must begin with `## 0.10.2`; the release workflow finds the section by that prefix and
uses it verbatim as the GitHub Release body. A released section is never edited afterwards — a fix
that lands later goes in the next version's section, because a release body that changes after
publication is a body nobody can trust.

## Build and verify locally before tagging

```bash
python -m build
python -m venv /tmp/relcheck && /tmp/relcheck/bin/pip install dist/sigil_memory-0.10.2-py3-none-any.whl
/tmp/relcheck/bin/sigil --version
/tmp/relcheck/bin/sigil --vault /tmp/relcheck-vault init
/tmp/relcheck/bin/sigil mcp --help
```

`python -m build` needs the `build` package; it writes `dist/sigil_memory-0.10.2.tar.gz` and
`dist/sigil_memory-0.10.2-py3-none-any.whl` (the distribution is `sigil-memory`, whose normalized
file form uses an underscore). The install is the point: a wheel that installs but whose
console script cannot import itself is the failure this catches, and only running the installed
binary finds it. `sigil --version` must print the version you just set, and `init` must make a
vault that the same binary can use.

## Tag, then let CI build

```console
git tag -a v0.10.2 -m "sigil 0.10.2"
git push origin v0.10.2
```

The `release.yml` workflow runs on the tag: it builds the sdist and the wheel, checks that the
wheel's version matches the tag, installs the wheel into a clean venv on Python 3.10, 3.12 and
3.13, and only then creates the GitHub Release with both artifacts attached and the CHANGELOG
section as its body. A failed matrix means no release exists yet, so fix and re-tag first.

The same workflow carries a `pypi` job that publishes the built artifacts to PyPI with OIDC trusted
publishing (`environment: pypi`, no token stored). It only succeeds once the pending publisher for
project `sigil-memory` exists on PyPI (owner `AgentOrange81`, repo `sigil-memory`, workflow `release.yml`,
environment `pypi`); until then the GitHub Release is the install path.

## After the tag

Bump `main` to the next development version in `pyproject.toml`, `sigil/__init__.py` and
`sigil/plugin.yaml` (the `X.Y.ZaN` form this repo uses, e.g. `0.10.3a1`), so a later install from
`main` cannot claim to be the release — only the tagged artifact carries the number `0.10.2`.

```console
sed -i 's/^version = "0.10.2"/version = "0.10.3a1"/' pyproject.toml
sed -i 's/^__version__ = "0.10.2"/__version__ = "0.10.3a1"/' sigil/__init__.py
sed -i 's/^version: 0.10.2$/version: 0.10.3a1/' sigil/plugin.yaml
git commit -am "chore: 0.10.3a1 after the 0.10.2 tag"
git push origin main
```

## Rollback

Before anyone installs, a tag can be deleted and re-pointed — delete it locally and on the
remote, fix the commit, tag again. The release workflow does not run for a deleted tag, so no
artifact survives the mistake. After a wheel is in someone's hands, do not move `0.10.2`: cut
`0.10.3` with the fix and leave `0.10.2` as what it was.
