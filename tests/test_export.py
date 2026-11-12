import json

from sigil import export as export_mod
from sigil.store import Entry, write_entry


def build(cfg):
    old = Entry(kind="fact", state="promoted", summary="DOCP 3600 runs stable on the TRX40",
                evidence="one afternoon with no MCEs",
                ask="what memory config should the TRX40 run?", project="wraith")
    new = Entry(kind="decision", state="promoted",
                summary="DOCP 3200 is the stable config; 3600 threw 649 CCEs in one boot",
                evidence="journalctl -b -1 | grep -c MC25_STATUS -> 649",
                ask="why did the box reboot at 03:02?", project="wraith", supersedes=[old.id])
    quiet = Entry(kind="fact", state="promoted", summary="no ask means no sft example")
    raw = Entry(kind="capture", state="raw", summary="chatter", ask="should not be exported")
    for e in (old, new, quiet, raw):
        write_entry(cfg.root, e)
    return old, new


def test_sft_only_from_promoted_with_ask(cfg):
    build(cfg)
    out = cfg.root / "data" / "sft.jsonl"
    n = export_mod.export_sft(cfg, out)
    assert n == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(r["messages"][-1]["role"] == "assistant" for r in rows)
    assert all(r["meta"]["sigil"]["state"] == "promoted" for r in rows)
    assert "chatter" not in out.read_text()


def test_vague_ask_is_not_a_training_prompt(cfg):
    write_entry(cfg.root, Entry(kind="lesson", state="promoted", summary="some lesson",
                                evidence="proof", ask="did that fix it"))
    out = cfg.root / "data" / "vague.jsonl"
    assert export_mod.export_sft(cfg, out) == 0


def test_system_prompt_is_prepended(cfg):
    build(cfg)
    out = cfg.root / "data" / "sft_sys.jsonl"
    export_mod.export_sft(cfg, out, system="You are a memory-grounded agent.")
    row = json.loads(out.read_text().splitlines()[0])
    assert row["messages"][0] == {"role": "system", "content": "You are a memory-grounded agent."}


def test_preference_pairs_from_supersedes(cfg):
    old, new = build(cfg)
    out = cfg.root / "data" / "dpo.jsonl"
    n = export_mod.export_preferences(cfg, out)
    assert n == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["prompt"] == "why did the box reboot at 03:02?"
    assert "3200" in row["chosen"] and "3600" in row["rejected"]
    assert row["meta"]["superseded"]["entry"] == old.id
    assert "649" in row["meta"]["reason"]


def test_graph_export_files(cfg):
    from sigil import graph as graph_mod
    eid = graph_mod.add_edge(cfg, "quipu", "depends_on", "sqlite")
    graph_mod.invalidate(cfg, eid)
    out = cfg.root / "data" / "graph"
    report = export_mod.export_graph(cfg, out)
    assert report == {"nodes": 2, "edges": 1}
    edge = json.loads((out / "edges.jsonl").read_text().splitlines()[0])
    assert edge["live"] is False and edge["valid_to"]


def test_dataset_report(cfg):
    build(cfg)
    r = export_mod.dataset_report(cfg)
    assert r["entries"] == 4 and r["promoted"] == 3 and r["raw"] == 1
    assert r["sft_examples"] == 2
    assert r["preference_pairs"] == 1
    # `quiet` is promoted but carries no evidence, and the audit flags exactly that
    assert r["promoted_with_evidence"] == 2
    assert 0 < r["promotion_rate"] <= 1
