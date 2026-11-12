"""Where a vault keeps its index, whatever the vault was called.

A vault written before the rename keeps its derived state in `.quipu`; one written after it uses
`.sigil`. `config.meta_dir_for` picks the directory that is actually on disk, so the benchmarks read
the same place the product reads instead of assuming a name only half the vaults have.

A benchmark that finds no index must SAY SO. An index that is missing is not a corpus that scores
zero: it is a benchmark pointed at the wrong vault, and a zero published as a measurement is a lie
with a citation.
"""
from __future__ import annotations

from pathlib import Path

from sigil import config as config_mod


def index_db(vault: Path) -> Path:
    """The vault's index, inside whichever meta dir that vault actually has."""
    return config_mod.meta_dir_for(vault) / "index.db"


def require_index(vault: Path) -> Path:
    """The vault's index, or a loud failure — a missing index is never a score of zero."""
    db = index_db(vault)
    if not db.exists():
        raise SystemExit(f"no index at {db} — run `sigil reindex` in that vault first")
    return db
