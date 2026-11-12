import pytest

from sigil.store import Entry, find_entry, iter_entries, read_entry, write_entry


def test_roundtrip_preserves_fields_and_unicode(cfg):
    e = Entry(kind="decision", state="promoted", agent="glitch", project="quipu",
              summary="Chose markdown as the source of truth — a 3.5 MB FTS5 index rebuilds in 40 ms",
              evidence="`time quipu reindex` → 0.04s over 822 notes",
              ask="should the vault be sqlite-first?", importance=0.9,
              supersedes=["abc12345"], tags=["design", "storage"], body="Body with an em dash — and 日本語.")
    path = write_entry(cfg.root, e)
    back = read_entry(path)
    assert back.id == e.id
    assert back.kind == "decision" and back.state == "promoted"
    assert back.supersedes == ["abc12345"]
    assert back.tags == ["design", "storage"]
    assert "日本語" in back.body
    assert back.importance == pytest.approx(0.9)
    assert find_entry(cfg.root, e.id) == path


def test_invalid_kind_is_rejected():
    with pytest.raises(ValueError):
        Entry(kind="rambling")


def test_invalid_state_is_rejected():
    with pytest.raises(ValueError):
        Entry(state="probably")


def test_iter_entries_skips_unparseable(cfg):
    write_entry(cfg.root, Entry(kind="fact", summary="one real entry"))
    (cfg.root / "notes" / "broken.md").write_text("no frontmatter, no kind", encoding="utf-8")
    entries = [e for _p, e in iter_entries(cfg.root)]
    assert len(entries) == 1


def test_numeric_looking_strings_stay_strings(cfg):
    """CI caught this: id "04159069" came back as 4159069 — the leading zero was parsed away."""
    e = Entry(id="04159069", kind="fact", project="2026", summary="digit-only id", confidence=0.5)
    back = read_entry(write_entry(cfg.root, e))
    assert back.id == "04159069"
    assert back.project == "2026"
    assert isinstance(back.project, str)


def test_numeric_fields_stay_numbers(cfg):
    e = Entry(kind="fact", summary="numbers survive", importance=0.85, confidence=0.4)
    back = read_entry(write_entry(cfg.root, e))
    assert back.importance == 0.85 and back.confidence == pytest.approx(0.4)


def test_summary_with_colon_and_quotes_survives(cfg):
    e = Entry(kind="lesson", summary='He said: "never rsync a live sqlite file" — it corrupts')
    back = read_entry(write_entry(cfg.root, e))
    assert back.summary == e.summary
