"""Dataset loaders.

Two datasets, and a strict rule about them:

* ``synthetic``  - a generated corpus whose ground truth is known BY CONSTRUCTION. No network, no
  download: the corpus is a pure function of this file, so a run is reproducible from the git
  rev alone. It is not a claim about the world; it is a measuring instrument, and its job is to
  make a retrieval bug show up as a number instead of as an opinion.
* ``longmemeval`` - a local LongMemEval JSON file. NEVER downloaded. If the file is not there the
  loader says so and the run is skipped. A number for a dataset that was not run is not a
  number, it is a rumour, and this harness will not produce one.

The synthetic corpus is built to be *hard enough to be informative*: notes come in clusters of
eight that share a subject, a domain and a rare marker term, so a query built from the marker
has to pick between eight similar notes, and a query built from the subject alone matches dozens.
Four query kinds are generated, one per real retrieval intent:

    rare       a distinctive term + the domain. Eight candidates; BM25 decides which one wins.
    phrase     the subject + a two-word phrase from the note. The matcher ANDs words, not
               phrases, so the note that merely contains the words is a candidate too.
    paraphrase the same fact in different words. The words exist in the corpus, but they point
               at a different note: right words, wrong note.
    question   a natural question. Every token has to be present for the AND matcher to return
               anything at all, and an interrogative is in no note.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sigil.store import Entry

SYNTHETIC_NAME = "synthetic"
SYNTHETIC_VERSION = "v1"
LONGMEMEVAL_NAME = "longmemeval"

#: One cluster = one subject area: eight notes about the same subject, in the same domain, under
#: one project, all mentioning the same rare marker term.
#: (marker, subject, subject_synonym, domain, domain_synonym, project)
CLUSTERS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("kestrel", "postgres", "postgresql", "checkout service", "purchase pipeline", "atlas"),
    ("anvil", "redis", "in-memory cache", "session store", "login cache", "bramble"),
    ("basalt", "kafka", "event bus", "ingest pipeline", "stream bus", "cirrus"),
    ("cobalt", "docker", "container runtime", "build pipeline", "image build", "domino"),
    ("driftwood", "kubernetes", "cluster scheduler", "staging cluster", "k8s rollout", "elmwood"),
    ("ember", "terraform", "infra as code", "cloud estate", "infra rollout", "fjord"),
    ("falcon", "next.js", "web framework", "web app", "ui server", "gantry"),
    ("gossamer", "prisma", "data mapper", "database layer", "object mapper", "hollow"),
    ("harrow", "sqlalchemy", "sql mapper", "query layer", "reporting layer", "ingot"),
    ("juniper", "pytest", "test runner", "ci suite", "unit tests", "jubilee"),
    ("kelp", "fts5", "full text search", "search index", "text search", "kettle"),
    ("lantern", "bm25", "ranking function", "ranking", "relevance scoring", "larkspur"),
    ("marble", "embeddings", "vector index", "semantic search", "vector search", "meadow"),
    ("nimbus", "sqlite-vec", "vector extension", "vector index", "embedding store", "nutmeg"),
    ("obsidian", "jito", "bundle submitter", "landing path", "bundle relay", "orchard"),
    ("pewter", "solana", "chain", "settlement path", "on chain", "pickerel"),
    ("quarry", "pump.fun", "launchpad", "launch path", "token launch", "quill"),
    ("rowan", "promotion gate", "lifecycle gate", "promotion", "promotion rules", "rafter"),
    ("sable", "dataset export", "exporter", "training data", "dataset build", "sorrel"),
    ("thicket", "sft export", "sft writer", "supervised set", "instruction pairs", "tinder"),
    ("umber", "dpo pairs", "preference pairs", "preference data", "preference pairs", "umbra"),
    ("verdigris", "graph export", "graph writer", "graph snapshot", "node export", "verdant"),
    ("wharf", "reflection pass", "reflection", "session digest", "session summary", "willow"),
    ("xenon", "session hook", "hook", "end of session", "session end", "xylem"),
    ("yarrow", "minhash dedup", "dedup", "near duplicate", "duplicate merge", "zephyr"),
)

#: Two-word phrases. A note mentions one of them; the same phrase shows up in other clusters, so
#: a phrase query has real competition.
DETAIL_PHRASES: tuple[str, ...] = (
    "write path",
    "cold start",
    "lock contention",
    "queue depth",
    "schema drift",
    "index bloat",
    "retry storm",
    "cache stampede",
    "fanout width",
    "lease renewal",
)

#: Sentences every note of a cluster carries. Their words are everywhere in the corpus, so BM25
#: gives them almost no weight - which is exactly why the ranking between eight similar notes is
#: decided by something else.
SHARED_TEMPLATES: tuple[str, ...] = (
    "Everything about {marker} is written down in the {project} runbook.",
    "The {project} team keeps {marker} in the same deploy pipeline as everything else.",
    "Nobody changed {marker} without a rollback plan, and none has been needed since.",
)

#: One unique sentence per note, built from that note's detail phrase.
UNIQUE_TEMPLATES: tuple[str, ...] = (
    "{marker} kept the {detail} stable until the {subject} upgrade landed.",
    "We measured a {detail} regression after the {subject} change.",
    "The {subject} rollout touched {detail} twice and needed a rollback once.",
    "{marker} and {detail} are the two things to check before trusting {subject} here.",
)

#: Note kinds, rotated so a cluster is not eight notes of the same kind.
KIND_CYCLE = ("decision", "lesson", "conclusion", "fact", "handoff", "capture")
KIND_LABEL = {
    "decision": "Decision",
    "lesson": "Lesson",
    "conclusion": "Conclusion",
    "fact": "Fact",
    "handoff": "Handoff",
    "capture": "Capture",
}
AGENTS = ("dana", "morgan", "riley")
EVIDENCE = (
    "measured: 412 ms p95 on the staging box",
    "log line: config reloaded without a restart",
    "ticket: two retries, no data loss",
    "note from the on-call handover",
)


@dataclass(frozen=True)
class BenchQuery:
    """One query, the entry ids that answer it, and what kind of retrieval it exercises."""

    qid: str
    text: str
    gold: tuple[str, ...]
    kind: str


@dataclass
class Dataset:
    """A corpus plus its query set."""

    name: str
    version: str
    entries: list[Entry]
    queries: list[BenchQuery]
    notes: str = ""


def _entry_id(cluster: int, index: int) -> str:
    return f"syn{cluster:02d}{index:02d}"


def _created(cluster: int, index: int) -> str:
    base = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    return (base + timedelta(hours=cluster * 8 + index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detail_for(cluster: int, index: int) -> str:
    """The note's detail phrase: two phrases per cluster, so it is shared with one sibling."""
    offset = (cluster * 2) % len(DETAIL_PHRASES)
    return DETAIL_PHRASES[(offset + index % 2) % len(DETAIL_PHRASES)]


def _domain_synonym_appears(cluster: int, index: int) -> bool:
    """A third of the notes name their domain both ways, the way real notes do."""
    return index % 3 == 0


def _build_entries() -> list[Entry]:
    entries: list[Entry] = []
    for ci, (marker, subject, subject_syn, domain, domain_syn, project) in enumerate(CLUSTERS):
        for j in range(8):
            eid = _entry_id(ci, j)
            kind = KIND_CYCLE[(ci + j) % len(KIND_CYCLE)]
            detail = _detail_for(ci, j)
            label = KIND_LABEL[kind]
            alias = f" (a.k.a. the {domain_syn})" if _domain_synonym_appears(ci, j) else ""
            summary = f"{label}: {marker} {detail} for the {domain} in {project}{alias}"
            body_sentences = [
                f"The {subject} work for {project} covers the {domain}.",
                SHARED_TEMPLATES[j % len(SHARED_TEMPLATES)].format(marker=marker, project=project),
                UNIQUE_TEMPLATES[j % len(UNIQUE_TEMPLATES)].format(
                    marker=marker, detail=detail, subject=subject),
            ]
            # A third of the notes name the thing the way other people name it. That synonym has to
            # be IN the corpus, or a paraphrase query would only prove that the matcher needs every
            # word present - which the question class already measures. With it in the corpus, a
            # paraphrase query returns real notes, and sometimes the right one.
            if _domain_synonym_appears(ci, j):
                body_sentences.append(f"Runbooks still call it {subject_syn}.")
            # The domain synonym also shows up in a different cluster, so a paraphrase query
            # finds notes: the right words, the wrong note.
            if j == 0:
                other = CLUSTERS[(ci + 7) % len(CLUSTERS)]
                body_sentences.append(
                    f"The {other[3]} here overlaps with the {domain_syn} we run for {project}.")
            if j % 4 == 1:
                body_sentences.append(f"See also the {subject} notes in {project}.")
            entries.append(Entry(
                id=eid,
                kind=kind,
                state="promoted" if j % 4 != 3 else "raw",
                agent=AGENTS[(ci + j) % len(AGENTS)],
                project=project,
                session=f"s-{project}-{ci:02d}",
                created=_created(ci, j),
                confidence=0.5 + 0.05 * (j % 6),
                importance=round(0.3 + 0.1 * (j % 7), 2),
                summary=summary,
                evidence=EVIDENCE[(ci + j) % len(EVIDENCE)],
                body=" ".join(body_sentences),
                tags=[subject.split()[0], project],
            ))
    return entries


def _build_queries() -> list[BenchQuery]:
    """One query of each kind per cluster. The gold rotates, so no class is aligned with the
    length of its gold note - BM25's bias toward short documents hits every class equally."""
    queries: list[BenchQuery] = []
    for ci, (marker, subject, subject_syn, domain, domain_syn, project) in enumerate(CLUSTERS):
        gold_index = ci % 8
        gold = (_entry_id(ci, gold_index),)
        detail = _detail_for(ci, gold_index)
        queries.append(BenchQuery(
            qid=f"q{ci:02d}-rare",
            text=f"{marker} {domain}",
            gold=gold,
            kind="rare",
        ))
        queries.append(BenchQuery(
            qid=f"q{ci:02d}-phrase",
            text=f"{subject} {detail}",
            gold=gold,
            kind="phrase",
        ))
        queries.append(BenchQuery(
            qid=f"q{ci:02d}-paraphrase",
            text=f"{domain_syn} {subject_syn}",
            gold=gold,
            kind="paraphrase",
        ))
        queries.append(BenchQuery(
            qid=f"q{ci:02d}-question",
            text=f"why did we choose {subject} for {project}",
            gold=gold,
            kind="question",
        ))
    return queries


def load_synthetic() -> Dataset:
    """The generated corpus. Deterministic: same file in, same corpus out, every time."""
    entries = _build_entries()
    queries = _build_queries()
    return Dataset(
        name=SYNTHETIC_NAME,
        version=SYNTHETIC_VERSION,
        entries=entries,
        queries=queries,
        notes=(
            f"{len(entries)} generated notes in {len(CLUSTERS)} clusters of eight; "
            f"{len(queries)} queries, four kinds per cluster."
        ),
    )


def load_longmemeval(path: str | Path) -> Dataset | None:
    """Load a local LongMemEval JSON file, or return None when it is not there.

    Accepts the shapes the dataset ships in: a list of items, or an object with an ``items`` key.
    Each item carries a question, the answer, and the haystack sessions the answer lives in; each
    session becomes one note and the note that contains the answer string is the gold.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"{p}: expected a list of items or an object with an 'items' key")
    entries: list[Entry] = []
    queries: list[BenchQuery] = []
    for idx, item in enumerate(items):
        answer = str(item.get("answer") or "")
        sessions = item.get("haystack_sessions") or item.get("sessions") or []
        gold: list[str] = []
        for sidx, session in enumerate(sessions):
            turns = session.get("turns") or session.get("messages") or []
            text = "\n".join(str(t.get("content", "")) for t in turns if isinstance(t, dict))
            if not text.strip():
                continue
            eid = f"lme{idx:04d}{sidx:04d}"
            entries.append(Entry(
                id=eid,
                kind="capture",
                state="promoted",
                agent="dataset",
                project=str(item.get("question_id", f"item-{idx}")),
                created=f"2026-01-01T00:00:{(idx + sidx) % 60:02d}Z",
                summary=str(session.get("session_id", eid))[:120],
                body=text,
            ))
            if answer and answer.lower() in text.lower():
                gold.append(eid)
        if not gold:
            continue
        queries.append(BenchQuery(
            qid=str(item.get("question_id", f"item-{idx}")),
            text=str(item.get("question", "")),
            gold=tuple(gold),
            kind="longmemeval",
        ))
    version = str(raw.get("version", "")) if isinstance(raw, dict) else ""
    return Dataset(
        name=LONGMEMEVAL_NAME,
        version=version or "local",
        entries=entries,
        queries=queries,
        notes=f"{len(items)} items, {len(entries)} session notes, {len(queries)} answerable questions.",
    )


def query_set_sha256(queries: list[BenchQuery]) -> str:
    """A digest of the query set, so a result file names the exact questions it was scored on."""
    blob = json.dumps(
        [[q.qid, q.text, list(q.gold), q.kind] for q in queries],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def corpus_sha256(entries: list[Entry]) -> str:
    """A digest of the corpus, for the same reason."""
    blob = json.dumps(
        [[e.id, e.kind, e.state, e.summary, e.body] for e in entries],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
