"""The promotion gate behind the write tool (#79).

`sigil_write` used to go through `sigil.mcp._write` with no `state` and no `importance`, so an entry an
agent wrote was always `raw` — present and searchable, but never promotable and never exported as
training data. The fix has to expose both fields *without* handing the model a way to mint `promoted`
entries, so the same gate the importer and `quipu promote` use decides: evidence **and** importance at
or above the vault's `promote_threshold`.
"""

from __future__ import annotations

import json
from pathlib import Path

import sigil.config
import sigil.index
from sigil.hermes_provider import SigilMemoryProvider

SUMMARY = "The release wheel refused to install until the filename kept all five parts"


def _provider(tmp_path: Path, monkeypatch) -> tuple[SigilMemoryProvider, Path]:
    vault = tmp_path / "vault"
    sigil.config.load(vault, create=True)
    monkeypatch.setenv("QUIPU_VAULT", str(vault))
    provider = SigilMemoryProvider()
    provider.initialize("gate-test", hermes_home=str(tmp_path / ".hermes"), platform="cli")
    return provider, vault


def _write(provider: SigilMemoryProvider, **args) -> dict:
    out = provider.handle_tool_call("sigil_write", args)
    assert isinstance(out, str), "the contract returns a JSON string"
    return json.loads(out)


def _state_on_disk(vault: Path, entry_id: str) -> str:
    """Read the entry the tool says it wrote, from the markdown, not from the reply."""
    for path in (vault / "notes").glob("*.md"):
        text = path.read_text()
        if entry_id in path.name:
            for line in text.splitlines():
                if line.startswith("state:"):
                    return line.split(":", 1)[1].strip().strip('"')
    raise AssertionError(f"entry {entry_id} not found on disk")


def test_evidence_and_high_importance_promotes(tmp_path, monkeypatch):
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="pip install q.whl -> Invalid wheel filename",
                 importance=0.9)
    assert out["state"] == "promoted"
    assert out["importance"] == 0.9
    assert _state_on_disk(vault, out["id"]) == "promoted"


def test_evidence_alone_stays_raw(tmp_path, monkeypatch):
    """No importance means no gate cleared: the entry is kept, searchable, and raw."""
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="same command, same error")
    assert out["state"] == "raw"
    assert _state_on_disk(vault, out["id"]) == "raw"


def test_asking_for_promoted_without_the_gate_is_downgraded_and_says_why(tmp_path, monkeypatch):
    """The tool cannot mint a promoted entry, and it tells the caller instead of failing silently."""
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="evidence is present", importance=0.1,
                 state="promoted")
    assert out["state"] == "raw"
    assert _state_on_disk(vault, out["id"]) == "raw"
    assert "promotion needs evidence and importance >=" in out["note"]
    assert "importance=0.1" in out["note"]


def test_explicit_pruned_is_honoured(tmp_path, monkeypatch):
    """Pruning is a deliberate act, not a gate question — but the entry still has to exist first."""
    provider, vault = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="superseded by the fix in #80", state="pruned")
    assert out["state"] == "pruned"
    assert _state_on_disk(vault, out["id"]) == "pruned"


def test_summary_is_still_required(tmp_path, monkeypatch):
    provider, _ = _provider(tmp_path, monkeypatch)
    out = _write(provider, evidence="no summary here")
    assert "error" in out and "summary" in out["error"]


def test_the_written_entry_is_searchable_afterwards(tmp_path, monkeypatch):
    """A gate that promotes but does not reindex is a silent memory hole."""
    provider, _ = _provider(tmp_path, monkeypatch)
    out = _write(provider, summary=SUMMARY, evidence="renamed the asset to five parts",
                 importance=0.9)
    hits = json.loads(provider.handle_tool_call("sigil_search", {"query": "wheel filename"}))
    assert any(h["id"] == out["id"] for h in hits["results"]), hits
