"""A vault written before the rename must open where it lies, unchanged.

The rename moved the package, not the user's data. `.quipu/` holds the index, the config and the capture
ledger; if the code only looked for `.sigil/`, then upgrading a package would make a vault look empty —
the worst possible failure for a memory tool, because it reports "no memories" instead of an error.
"""
from __future__ import annotations

from sigil.config import LEGACY_META_DIR, META_DIR, Config, load, meta_dir_for


def _legacy_vault(tmp_path, section: str = "quipu"):
    """A vault shaped exactly like one written by 0.8.0: `.quipu/` with a `[quipu]` config table."""
    root = tmp_path / "old-vault"
    (root / "notes").mkdir(parents=True)
    (root / "entities").mkdir()
    meta = root / LEGACY_META_DIR
    meta.mkdir()
    (meta / "config.toml").write_text(
        f'[{section}]\npromote_threshold = 0.75\nmax_promoted_tokens = 1234\npredicates = ["uses"]\n',
        encoding="utf-8",
    )
    return root


def test_a_vault_that_still_has_the_old_internal_dir_opens(tmp_path):
    root = _legacy_vault(tmp_path)
    cfg = load(root)

    assert cfg.meta_dir == root / LEGACY_META_DIR
    assert cfg.db_path == root / LEGACY_META_DIR / "index.db"
    assert cfg.config_path.exists()


def test_the_old_config_section_still_parses(tmp_path):
    cfg = load(_legacy_vault(tmp_path, section="quipu"))

    assert cfg.promote_threshold == 0.75
    assert cfg.max_promoted_tokens == 1234
    assert cfg.predicates == ("uses",)


def test_the_new_section_parses_too(tmp_path):
    """So a vault can be migrated by renaming its directory and its table, in that order or the other."""
    cfg = load(_legacy_vault(tmp_path, section="sigil"))

    assert cfg.promote_threshold == 0.75


def test_a_new_vault_is_written_under_the_new_name(tmp_path):
    cfg = load(tmp_path / "fresh", create=True)

    assert cfg.meta_dir == tmp_path / "fresh" / META_DIR
    assert f"[{META_DIR}]" not in (cfg.config_path).read_text(encoding="utf-8")  # table is [sigil]
    assert "[sigil]" in cfg.config_path.read_text(encoding="utf-8")


def test_writing_to_a_legacy_vault_does_not_relocate_it(tmp_path):
    """Opening an old vault must never create `.sigil/` beside `.quipu/` — that splits the vault in two
    and the next command reads the empty half."""
    root = _legacy_vault(tmp_path)
    cfg = load(root)
    cfg.save()

    assert (root / LEGACY_META_DIR / "config.toml").exists()
    assert not (root / META_DIR).exists()


def test_the_helper_prefers_the_directory_that_exists(tmp_path):
    root = tmp_path / "v"
    (root / LEGACY_META_DIR).mkdir(parents=True)
    assert meta_dir_for(root) == root / LEGACY_META_DIR

    (root / META_DIR).mkdir()
    assert meta_dir_for(root) == root / META_DIR, "once migrated, the new name wins"


def test_a_bare_config_object_still_points_somewhere_sane(tmp_path):
    """`Config(root=...)` is constructed directly in tests and by callers; it must not raise."""
    cfg = Config(root=str(tmp_path / "bare"))

    assert cfg.meta_dir == tmp_path / "bare" / META_DIR
