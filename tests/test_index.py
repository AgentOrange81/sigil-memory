from sigil import index as index_mod
from sigil.store import Entry, write_entry


def seed(cfg):
    entries = [
        Entry(kind="decision", state="promoted", project="quipu", agent="glitch",
              summary="Markdown is the source of truth", evidence="40 ms full reindex",
              body="sqlite is a cache"),
        Entry(kind="lesson", state="promoted", project="gpu-setup", agent="hermes",
              summary="Wan 2.2 video VAE decode hangs the ring on gfx1031",
              evidence="journalctl -b -1"),
        Entry(kind="capture", state="raw", project="quipu", agent="glitch",
              summary="raw note about markdown exporters"),
    ]
    for e in entries:
        write_entry(cfg.root, e)
    return entries


def test_reindex_and_search_rank(cfg):
    seed(cfg)
    n = index_mod.reindex(cfg.root, cfg.db_path)
    assert n == 3
    rows = index_mod.search(cfg.db_path, "markdown")
    assert len(rows) == 2
    assert all("markdown" in (r["summary"] + r["body"]).lower() for r in rows)


def test_filters(cfg):
    seed(cfg)
    index_mod.reindex(cfg.root, cfg.db_path)
    assert len(index_mod.search(cfg.db_path, "markdown", project="quipu")) == 2
    assert len(index_mod.search(cfg.db_path, "markdown", state="promoted")) == 1
    assert len(index_mod.search(cfg.db_path, "markdown", agent="hermes")) == 0
    assert index_mod.search(cfg.db_path, "kernel") == []


def test_prefix_match_on_last_token(cfg):
    seed(cfg)
    index_mod.reindex(cfg.root, cfg.db_path)
    assert index_mod.search(cfg.db_path, "mardown") == []       # typo: no match
    assert index_mod.search(cfg.db_path, "markdo") != []        # prefix: matches


def test_stats(cfg):
    seed(cfg)
    index_mod.reindex(cfg.root, cfg.db_path)
    s = index_mod.stats(cfg.db_path)
    assert s["entries"] == 3
    assert s["by_state"]["promoted"] == 2
