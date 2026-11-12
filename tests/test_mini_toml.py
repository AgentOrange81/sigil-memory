"""Inline comments in `config.toml`: `_mini_toml` — the Python 3.10 parser — must tolerate the
trailing comments an operator writes, keep `#` inside quoted strings, and leave a comment-free
file parsed exactly as before. The cross-parser comparison half needs tomllib (3.11+); the
`_mini_toml` assertions run on every interpreter.
"""
import pytest

from sigil import config as config_mod

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the fallback parser is the only parser
    tomllib = None

#: What `Config.save()` writes, before anyone adds a comment: the exact TOML subset.
SUBSET_WITHOUT_COMMENTS = (
    "[quipu]\n"
    "promote_threshold = 0.7\n"
    "max_promoted_tokens = 5000\n"
    'predicates = [\n  "a",\n  "b",\n]\n'
    "\n[llm]\n"
    'base_url = "http://127.0.0.1:8080/v1"\n'
    'model = "local"\n'
    'api_key = ""\n'
    "timeout = 30\n"
    "\n[embed]\n"
    'url = ""\n'
    'model = "nomic-embed"\n'
)

#: The same file with a trailing comment on every line, `#` inside a quoted string, and a
#: comment after the closing bracket of the array — what an operator actually ends up with.
SUBSET_FULLY_COMMENTED = (
    "# vault config — every editable line carries a comment\n"
    "[quipu]\n"
    "promote_threshold = 0.5  # keep\n"
    "max_promoted_tokens = 12000   # cap\n"
    'predicates = [\n  "a",\n  "b",\n]  # end\n'
    "\n[llm]\n"
    'base_url = "http://h/#anchor"  # prod\n'
    'model = "local"  # default\n'
    'api_key = ""  # none\n'
    "timeout = 240 # seconds\n"
    "\n[embed]\n"
    'url = ""  # lexical-only\n'
    'model = "nomic-embed"  # default\n'
)


def test_commented_float_scalar():
    """`promote_threshold = 0.5  # keep` parses as the float 0.5, comment gone."""
    data = config_mod._mini_toml("[quipu]\npromote_threshold = 0.5  # keep\n")
    assert data == {"quipu": {"promote_threshold": 0.5}}


def test_commented_int_scalar():
    """`max_promoted_tokens = 12000   # cap` parses as the int 12000, comment gone."""
    data = config_mod._mini_toml("[quipu]\nmax_promoted_tokens = 12000   # cap\n")
    assert data == {"quipu": {"max_promoted_tokens": 12000}}


def test_commented_llm_timeout():
    """`timeout = 240 # seconds` inside [llm] parses as the int 240, comment gone."""
    data = config_mod._mini_toml("[llm]\ntimeout = 240 # seconds\n")
    assert data == {"llm": {"timeout": 240}}


def test_hash_inside_quoted_string():
    """A `#` inside a quoted string is data; a trailing comment behind the string is not."""
    data = config_mod._mini_toml('[llm]\nbase_url = "http://h/#anchor"\n')
    assert data == {"llm": {"base_url": "http://h/#anchor"}}
    commented = config_mod._mini_toml('[llm]\nbase_url = "http://h/#anchor"  # prod\n')
    assert commented == {"llm": {"base_url": "http://h/#anchor"}}


def test_multiline_array_and_comment_after_closing_bracket():
    """The multi-line array save() writes parses, and `]  # end` behind the bracket does not."""
    data = config_mod._mini_toml('[quipu]\npredicates = [\n  "a",\n  "b",\n]\n')
    assert data == {"quipu": {"predicates": ["a", "b"]}}
    commented = config_mod._mini_toml('[quipu]\npredicates = [\n  "a",\n  "b",\n]  # end\n')
    assert commented == {"quipu": {"predicates": ["a", "b"]}}


def test_leading_full_line_comment_ignored():
    """A full-line leading comment is dropped, and the file still parses."""
    data = config_mod._mini_toml("# note\n[quipu]\npromote_threshold = 0.5\n")
    assert data == {"quipu": {"promote_threshold": 0.5}}


def test_subset_without_comments_unchanged():
    """A comment-free file — the save() subset — parses to exactly the same values as before."""
    expected = {
        "quipu": {
            "promote_threshold": 0.7,
            "max_promoted_tokens": 5000,
            "predicates": ["a", "b"],
        },
        "llm": {
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "local",
            "api_key": "",
            "timeout": 30,
        },
        "embed": {"url": "", "model": "nomic-embed"},
    }
    assert config_mod._mini_toml(SUBSET_WITHOUT_COMMENTS) == expected


def test_load_fully_commented_config(cfg, monkeypatch):
    """A fully commented config.toml in a temp vault loads to the same values under both parsers.

    `config_mod.tomllib is None` simulates Python 3.10 — the same pattern test_load_without_tomllib
    uses. The direct `tomllib.loads` comparison is skipped on 3.10; the `_mini_toml` assertions of
    both load paths above it still run there.
    """
    cfg.config_path.write_text(SUBSET_FULLY_COMMENTED, encoding="utf-8")

    modern = config_mod.load(cfg.root)  # tomllib where it exists, _mini_toml on 3.10
    assert modern.promote_threshold == 0.5
    assert modern.max_promoted_tokens == 12000
    assert modern.llm_timeout == 240

    monkeypatch.setattr(config_mod, "tomllib", None)
    mini_load = config_mod.load(cfg.root)  # the 3.10 load path, forced on a modern interpreter too
    assert mini_load.promote_threshold == 0.5
    assert mini_load.max_promoted_tokens == 12000
    assert mini_load.llm_timeout == 240

    text = cfg.config_path.read_text(encoding="utf-8")
    mini = config_mod._mini_toml(text)  # the 3.10 parser, called directly on the text
    assert mini["quipu"]["promote_threshold"] == 0.5
    assert mini["quipu"]["max_promoted_tokens"] == 12000
    assert mini["llm"]["timeout"] == 240

    if tomllib is None:
        pytest.skip("tomllib is 3.11+; every _mini_toml and load assertion above already ran")
    parsed = tomllib.loads(text)
    assert parsed["quipu"]["promote_threshold"] == mini["quipu"]["promote_threshold"]
    assert parsed["quipu"]["max_promoted_tokens"] == mini["quipu"]["max_promoted_tokens"]
    assert parsed["llm"]["timeout"] == mini["llm"]["timeout"]