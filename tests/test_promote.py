import json

import pytest

from sigil import graph as graph_mod
from sigil import promote as promote_mod
from sigil.store import iter_entries

GOOD = [{
    "kind": "decision",
    "summary": "DOCP 3200 is the stable memory config; 3600 threw 649 CCE events in one boot",
    "body": "Dropped FCLK to 1600 to keep 1:1 with 3200.",
    "evidence": "journalctl -b -1 | grep -c MC25_STATUS -> 649",
    "importance": 0.9,
    "ask": "why did the box reboot?",
    "entities": [{"name": "trx40", "kind": "machine"}, {"name": "docp-3200", "kind": "config"}],
    "edges": [{"src": "trx40", "predicate": "measured_as", "dst": "docp-3200"}],
}]

WEAK = [{
    "kind": "fact",
    "summary": "The user seems to like concise answers",
    "importance": 0.2,
    "evidence": "",
}]


def test_promoted_requires_threshold_and_evidence(cfg):
    proposals = promote_mod.parse_response(json.dumps({"entries": GOOD + WEAK}))
    written = promote_mod.apply(proposals, cfg, agent="glitch", project="wraith")
    assert [e.state for e in written] == ["promoted", "raw"]


def test_graph_only_learns_from_promoted(cfg):
    proposals = promote_mod.parse_response(json.dumps({"entries": GOOD + WEAK}))
    promote_mod.apply(proposals, cfg, agent="glitch")
    names = {n["name"] for n in graph_mod.neighbors(cfg, "trx40")["nodes"]}
    assert names == {"trx40", "docp-3200"}


def test_parse_response_tolerates_fences_and_prose():
    raw = 'Sure! Here you go:\n```json\n{"entries": ' + json.dumps(GOOD) + '}\n```\nHope that helps'
    assert promote_mod.parse_response(raw)[0].kind == "decision"


def test_parse_response_rejects_garbage():
    with pytest.raises(ValueError):
        promote_mod.parse_response("no json here")


def test_reflect_uses_provider(cfg):
    provider = promote_mod.FakeProvider(GOOD)
    proposals = promote_mod.reflect("a transcript about ram", cfg, provider)
    assert proposals[0].entities[0] == ("trx40", "machine")


def test_unknown_predicate_is_skipped_not_fatal(cfg):
    bad = dict(GOOD[0])
    bad["edges"] = [{"src": "trx40", "predicate": "vibes_with", "dst": "docp-3200"}]
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [bad]})), cfg)
    assert written[0].state == "promoted"
    assert graph_mod.timeline(cfg, "trx40") == []


def test_record_capture_is_raw_and_searchable(cfg):
    from sigil import index as index_mod
    e = promote_mod.record_capture(cfg, "raw thought about exporters", agent="glitch")
    assert e.state == "raw" and e.kind == "capture"
    index_mod.reindex(cfg.root, cfg.db_path)
    assert index_mod.search(cfg.db_path, "exporters")


def test_capture_with_empty_summary_still_writes(cfg):
    e = promote_mod.record_capture(cfg, "x")
    assert any(x.id == e.id for _p, x in iter_entries(cfg.root))


def test_a_proposal_with_a_quoted_summary_is_written_as_a_handle(cfg):
    """A measured reflect run emitted exactly this shape and it went straight to disk (#85)."""
    quoted = dict(GOOD[0])
    quoted["summary"] = '"' + GOOD[0]["summary"] + '"'
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [quoted]})), cfg)
    assert written[0].summary == GOOD[0]["summary"]


def test_a_proposal_with_a_blob_summary_takes_the_body_statement(cfg):
    blob = dict(GOOD[0])
    blob["summary"] = '{"findings": [{"claim": "the lightbox lives in HistoryPage"}]}'
    blob["body"] = "The lightbox is rendered in HistoryPage.tsx around line 106, with an onClick handler."
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [blob]})), cfg)
    assert written[0].summary == (
        "The lightbox is rendered in HistoryPage.tsx around line 106, with an onClick handler.")


def test_a_blob_proposal_with_no_prose_body_keeps_the_entry(cfg):
    """The body has no statement either, so the entry survives with the text it arrived with: a
    mis-shaped handle costs the handle, never the memory."""
    blob = dict(GOOD[0])
    blob["summary"] = '{"findings": []}'
    blob["body"] = '{"other": 1}'
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [blob]})), cfg)
    assert written[0].summary == '{"findings": []}'


# --- #103: a proposal's edges face the same closed vocabulary as every other writer -------------

def test_an_out_of_vocabulary_edge_is_neither_written_nor_asserted(cfg):
    proposal = {
        "kind": "fact", "summary": "the audit reads the markdown, not only the rows",
        "evidence": "pytest tests/test_promote.py", "importance": 0.9,
        "ask": "what does audit validate?", "entities": [],
        "edges": [{"src": "audit", "predicate": "implements", "dst": "markdown-check"},
                  {"src": "main", "predicate": "implemented_by", "dst": "quipu"}],
        "confidence": 0.8,
    }
    report = {}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg,
                                report=report)
    assert len(written) == 1, "a bad edge costs the claim, never the memory"
    assert written[0].edges == ["audit:implements:markdown-check"], written[0].edges
    assert report["skipped_edges"] == ["main:implemented_by:quipu"], report
    from sigil import graph as graph_mod
    live = [r["predicate"] for r in graph_mod.timeline(cfg, "audit")]
    assert live == ["implements"]


def test_a_clean_proposal_reports_nothing(cfg):
    report = {}
    promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": GOOD})), cfg, report=report)
    assert report["skipped_edges"] == []


def test_a_supersedes_that_names_no_entry_is_dropped_and_reported(cfg):
    """A real pass proposed `supersedes: "Re-measured after engine 0.1.56, …"` — prose in an id field.
    Storing it makes the pair reference nothing and the preference export silently lose it."""
    from sigil.store import Entry, write_entry
    real = Entry(id="abcd1234", state="promoted", summary="the belief being corrected",
                 evidence="fixture")
    write_entry(cfg.root, real)
    proposal = {
        "kind": "decision", "summary": "the belief is now different", "evidence": "fixture",
        "importance": 0.9, "ask": "what changed?", "entities": [],
        "edges": [], "supersedes": ["abcd1234", "Re-measured after engine 0.1.56, llama.cpp"],
        "confidence": 0.8,
    }
    report = {}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg,
                                report=report)
    assert written[0].supersedes == ["abcd1234"], written[0].supersedes
    assert report["skipped_supersedes"] == ["Re-measured after engine 0.1.56, llama.cpp"]


# --- #105: what a model emits vs what a spec can hold ------------------------------------------

def test_a_response_with_a_control_character_in_a_string_parses():
    """A small local model puts a literal newline inside a JSON string; strict json.loads refuses it
    and the whole pass is lost. Measured: 2 of 27 sessions failed this way on the 35B tier."""
    raw = '{"entries": [{"kind": "fact", "summary": "line one\nline two is the same thought", ' \
          '"evidence": "fixture", "importance": 0.9}]}'
    proposals = promote_mod.parse_response(raw)
    assert len(proposals) == 1
    assert "line one" in proposals[0].summary


def test_an_edge_whose_part_holds_the_separator_is_refused(cfg):
    """`GraphRAG:requires:enable_thinking:false` re-parses as four parts, so a dst containing a colon
    cannot round-trip. Measured on a real pass: the spec went to the markdown and the replay refused
    it (#105). The claim is dropped and reported, like any other unassertable one."""
    proposal = {
        "kind": "fact", "summary": "thinking is disabled for that graph query",
        "evidence": "fixture", "importance": 0.9, "ask": "why is thinking disabled?",
        "entities": [],
        "edges": [{"src": "GraphRAG", "predicate": "depends_on", "dst": "enable_thinking:false"}],
        "confidence": 0.8,
    }
    report = {}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg,
                                report=report)
    assert written[0].edges == [], "a spec that cannot round-trip is not storable"
    assert report["skipped_edges"] == ["GraphRAG:depends_on:enable_thinking:false"]


def test_a_refused_edge_never_reaches_the_graph_either(cfg):
    """#112: the frontmatter list was gated but the graph-assert loop walked the raw proposals, so
    `add_edge(record=True)` wrote the refused spec back into the markdown. Measured live: entry
    cb000190 carried `vector:implements:gpt-oss:120b` (4 parts, unparseable on replay) while the graph
    held the sanitised `gpt-oss-120b` — two shapes of one claim, and audit reporting both."""
    from sigil import graph as graph_mod
    proposal = {
        "kind": "decision", "summary": "switched the QA lane to gpt-oss:120b",
        "evidence": "fixture", "importance": 0.9, "ask": "which model runs QA?", "entities": [],
        "edges": [{"src": "vector", "predicate": "implements", "dst": "gpt-oss:120b"},
                  {"src": "vector", "predicate": "not_a_predicate", "dst": "something"}],
        "confidence": 0.8,
    }
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg)
    assert written[0].edges == [], written[0].edges
    live = graph_mod.timeline(cfg, "vector")
    assert live == [], f"refused edges must not reach the graph, got {live}"
    on_disk = [p for p in cfg.root.rglob("*.md") if "gpt-oss" in p.read_text(encoding="utf-8")
               and "edges:" in p.read_text(encoding="utf-8")]
    assert on_disk == [], f"no markdown should record a refused spec: {on_disk}"


# --- #110: an entry records the model that wrote it ----------------------------------------------

def test_the_model_that_wrote_an_entry_is_recorded_and_survives_a_round_trip(cfg):
    from sigil import index as index_mod
    from sigil.store import Entry
    proposal = {"kind": "fact", "summary": "recorded by a named model", "evidence": "fixture",
                "importance": 0.9, "ask": "which model wrote this?", "entities": [], "edges": [],
                "confidence": 0.8}
    written = promote_mod.apply(promote_mod.parse_response(json.dumps({"entries": [proposal]})), cfg,
                                model="qwen3.6-35b-a3b")
    assert written[0].model == "qwen3.6-35b-a3b"

    path = next(cfg.root.glob(f"notes/{written[0].id}*.md"))
    assert "model: qwen3.6-35b-a3b" in path.read_text(encoding="utf-8")
    assert Entry.from_markdown(path.read_text(encoding="utf-8")).model == "qwen3.6-35b-a3b"

    index_mod.reindex(cfg.root, cfg.db_path)
    import sqlite3
    with sqlite3.connect(cfg.db_path) as c:
        row = c.execute("SELECT model FROM entries WHERE id = ?", (written[0].id,)).fetchone()
    assert row and row[0] == "qwen3.6-35b-a3b"
