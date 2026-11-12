"""Fabric frontmatter: single-quoted and folded scalars (#54).

Fabric's own writer emits summaries as YAML **single-quoted** scalars and wraps long ones onto a second
line. The importer was line-based and only understood `"…"` and `[…]`, so every such summary arrived
with its opening quote still attached and cut off at the wrap.

That is not cosmetic. Measured on the live vault: 88 entries carry a leading quote (50 promoted), and the
20-entry `'Corrected metadata for <topic> (agent: …, project_id:` family — one truncated identical prefix
— supplied 190 of the 206 high-similarity pairs in a semantic sweep. Any similarity-based feature would
have clustered that damage first.

These tests drive the parser through `_split_frontmatter`, the surface a real import uses, and then a
full `import_fabric` run to prove the whole path comes out clean.
"""

import textwrap

import pytest

from sigil.importers.fabric import _parse_value, _split_frontmatter, import_fabric
from sigil.store import iter_entries


def fm_of(text: str) -> dict:
    # the fixtures start with a newline for readability; frontmatter only counts at byte 0
    fm, _body = _split_frontmatter(textwrap.dedent(text).lstrip("\n"))
    assert fm is not None, "fixture has no frontmatter at all"
    return fm


def test_a_single_quoted_summary_loses_its_quotes():
    fm = fm_of("""
        ---
        id: abcd1234
        summary: 'A plain claim about the glitch box'
        ---
        body
    """)
    assert fm["summary"] == "A plain claim about the glitch box"


def test_a_wrapped_single_quoted_summary_is_folded_whole():
    """The defect itself: the wrap used to truncate the value and keep the quote."""
    fm = fm_of("""
        ---
        id: 0945cd17
        summary: 'Corrected metadata for PRL mining fleet details (agent: hermes, project_id:
          prl-mining)'
        type: resolution
        ---
        body
    """)
    assert fm["summary"] == ("Corrected metadata for PRL mining fleet details (agent: hermes, "
                             "project_id: prl-mining)")
    assert not fm["summary"].startswith("'")


def test_a_doubled_apostrophe_is_a_literal_one():
    fm = fm_of("""
        ---
        id: abcd1234
        summary: 'the agent''s box, and the user''s rules'
        ---
        body
    """)
    assert fm["summary"] == "the agent's box, and the user's rules"


def test_an_unquoted_summary_with_an_apostrophe_is_left_alone():
    """A bare scalar that merely contains a quote must not be treated as an unterminated one."""
    fm = fm_of("""
        ---
        id: abcd1234
        summary: the agent's box runs at 3200 fclk
        ---
        body
    """)
    assert fm["summary"] == "the agent's box runs at 3200 fclk"


def test_double_quoted_and_inline_list_still_parse():
    fm = fm_of("""
        ---
        id: abcd1234
        summary: "A \\"quoted\\" claim"
        tags: ["a", "b"]
        ---
        body
    """)
    assert fm["summary"] == 'A "quoted" claim'
    assert fm["tags"] == ["a", "b"]


def test_a_folded_value_keeps_the_fields_that_follow_it():
    fm = fm_of("""
        ---
        id: abcd1234
        summary: 'one long claim that wraps
          onto a second line'
        project_id: quipu
        ---
        body
    """)
    assert fm["summary"] == "one long claim that wraps onto a second line"
    assert fm["project_id"] == "quipu", "the line after a folded scalar was swallowed"


def test_an_unterminated_quote_still_yields_a_value():
    """A truncated source file must not cost the entry: take what is there."""
    fm = fm_of("""
        ---
        id: abcd1234
        summary: 'a summary whose closing quote never arrives
        project_id: quipu
        ---
        body
    """)
    assert fm.get("summary"), fm
    assert not fm["summary"].startswith("'"), fm["summary"]


def test_parse_value_handles_each_scalar_shape():
    assert _parse_value("'x'") == "x"
    assert _parse_value('"x"') == "x"
    assert _parse_value("x") == "x"
    assert _parse_value("") == ""


@pytest.fixture()
def fabric_dir(tmp_path):
    src = tmp_path / "fabric"
    src.mkdir()
    (src / "a.md").write_text(textwrap.dedent("""
        ---
        id: aaa11111
        agent: cipher
        platform: cli
        timestamp: '2026-10-01T10:00:00Z'
        type: decision
        tier: hot
        summary: 'Corrected metadata for the PRL mining fleet (agent: hermes, project_id:
          prl-mining)'
        project_id: prl-mining
        training_value: high
        evidence: 'verified by ls'
        ---

        body
    """).lstrip("\n"), encoding="utf-8")
    (src / "b.md").write_text(textwrap.dedent("""
        ---
        id: bbb22222
        agent: cipher
        platform: cli
        timestamp: '2026-10-02T10:00:00Z'
        type: decision
        tier: hot
        summary: "A double quoted summary"
        project_id: quipu
        training_value: high
        evidence: 'verified by a test'
        ---

        body
    """).lstrip("\n"), encoding="utf-8")
    return src


def test_a_full_import_leaves_no_quoted_summaries(fabric_dir, tmp_path):
    """Assert the PARSED handle, not the file's YAML style: the writer legitimately quotes a value that
    contains a colon, and `read_entry` is what turns that back into text."""
    vault = tmp_path / "vault"
    result = import_fabric(fabric_dir, vault)
    assert len(result.written) == 2, result
    summaries = [e.summary for _p, e in iter_entries(vault)]
    assert len(summaries) == 2, summaries
    bad = [s for s in summaries if s[:1] in ("'", '"')]
    assert bad == [], f"quoted summaries survived the import: {bad}"
    assert any(s.startswith("Corrected metadata for the PRL mining fleet") for s in summaries), summaries
