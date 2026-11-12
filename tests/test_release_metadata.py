"""The release invariants: one version, in three files, with a changelog section for it.

A wheel whose `sigil --version` disagrees with its own filename, a `hermes plugins list` row that
reports the previous release, or a tag with no `## <version>` heading to publish as release notes,
is the kind of mistake that is only visible to the person installing. So it is a test.
"""
import re
from pathlib import Path

import pytest

import sigil

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
PLUGIN_MANIFEST = ROOT / "sigil" / "plugin.yaml"


def _pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"$', PYPROJECT, re.M)
    assert match, "pyproject.toml has no [project] version line"
    return match.group(1)


def _manifest_field(field: str) -> str:
    match = re.search(rf"^{field}:\s*(\S+)\s*$", PLUGIN_MANIFEST.read_text(encoding="utf-8"), re.M)
    assert match, f"sigil/plugin.yaml has no top-level {field!r} line"
    return match.group(1).strip("\"'")


def test_the_package_and_the_metadata_agree_on_the_version():
    assert sigil.__version__ == _pyproject_version(), (
        "sigil/__init__.py and pyproject.toml disagree; `sigil --version` and the MCP "
        "serverInfo.version would both be wrong"
    )


def test_the_plugin_manifest_agrees_with_the_package_version():
    """`hermes plugins list` shows plugin.yaml's version, and nothing in the build reads it.

    That is exactly why it rots: a release that moves pyproject.toml and __init__.py leaves every
    profile advertising the previous number until someone happens to look at the table.
    """
    assert _manifest_field("version") == _pyproject_version(), (
        "sigil/plugin.yaml and pyproject.toml disagree; `hermes plugins list` would report a "
        "version the artifact does not carry"
    )


def test_the_plugin_manifest_names_the_package():
    assert _manifest_field("name") == "sigil", (
        "the manifest name is how Hermes discovers the plugin; it must match the package directory"
    )


def test_the_version_is_a_pep440_release_not_a_dev_marker():
    version = _pyproject_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"{version!r} is not a plain X.Y.Z release; tag only after the dev/a1 marker is gone"
    )


def test_the_changelog_has_a_section_for_the_shipped_version():
    version = _pyproject_version()
    assert re.search(rf"^## {re.escape(version)}\b", CHANGELOG, re.M), (
        f"no `## {version}` heading in CHANGELOG.md — the release notes are extracted from it"
    )


def test_no_unreleased_section_survives_a_release():
    version = _pyproject_version()
    if re.search(rf"^## {re.escape(version)}\b", CHANGELOG, re.M):
        assert not re.search(r"^## Unreleased\b", CHANGELOG, re.M) or CHANGELOG.index(
            "## Unreleased"
        ) < CHANGELOG.index(f"## {version}"), (
            "an `## Unreleased` section sits above the released one; move it below or fold it in"
        )


def test_the_release_workflow_exists_and_only_fires_on_tags():
    workflow = ROOT / ".github" / "workflows" / "release.yml"
    assert workflow.exists(), "no release workflow: a tag would publish nothing"
    text = workflow.read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in text, "the release workflow must be tag-triggered"
    for step in ("python -m build", "gh release create"):
        assert step in text, f"the release workflow no longer runs {step!r}"


@pytest.mark.parametrize("path", ["README.md", "docs/RELEASING.md"])
def test_the_docs_mention_the_current_version(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert _pyproject_version() in text or "sigil --version" in text, (
        f"{path} neither names the current version nor tells the reader to check it"
    )
