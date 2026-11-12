"""Markdown entry store — the source of truth.

Every memory is one file with YAML-ish frontmatter. No YAML dependency: scalars are written as
`key: value`, quoted only when needed, and lists as JSON arrays (which are valid YAML inline lists
for any other tool, Obsidian included).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import KINDS, STATES

FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
KV_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return os.urandom(4).hex()


#: ONE definition of "this looks like a number", shared by the writer and the reader. The writer
#: quotes every string that matches it (so it comes back as the string it was); the reader
#: coerces only UNQUOTED values that match it (so a quoted value is always a string).
#: Exponent forms are in: "1.5e3" reads back as 1500.0 unless the writer quotes it. Underscores
#: and a leading "+" are out: Python's int()/float() would take "1_0" for 10 and "+7" for 7, and
#: a string that only looks numeric must stay a string.
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")

#: Markers of a pasted tool transcript. A summary that carries one is the output of a tool, not
#: a claim anybody made (#30).
TRANSCRIPT_MARKERS = ("FILES:", "GATE:", "MAP:")

#: A path on its own - "sigil/graph.py", "/home/dirk/x.md" - is a file listing, not a sentence.
_BARE_PATH_RE = re.compile(r"^[\w.~@+-]+(?:/[\w.~@+-]+)*\.[A-Za-z0-9]{1,8}$")

#: A lowercase word of three letters or more: the mark of prose.
_PROSE_RE = re.compile(r"[a-z]{3,}")

#: The end of a sentence.
_SENTENCE_END_RE = re.compile(r"[.!?…]")


def looks_like_transcript(summary: str) -> bool:
    """Is this summary a pasted tool transcript rather than a claim? (#30)

    The summary is the retrieval signal: an agent reads it as a statement, so a pasted
    transcript reads as a statement too - and is worthless. It is caught at import and filed as
    `pruned` with a tag: visible, reversible, and never promoted.

    A transcript starts with a code fence, carries a tool marker (FILES:, GATE:, MAP:), spans
    more than one paragraph, is a bare path, or has no sentence structure at all.
    """
    s = (summary or "").strip()
    if not s:
        return False
    if s.startswith("```"):
        return True
    if any(marker in s for marker in TRANSCRIPT_MARKERS):
        return True
    if "\n\n" in s:
        return True
    if _BARE_PATH_RE.match(s):
        return True
    if not _SENTENCE_END_RE.search(s) and not _PROSE_RE.findall(s):
        return True
    return False


#: A summary is the retrieval handle: it is what ranks, what a brief line shows, and what an SFT example
#: is made of. Past this size it stops being a handle and becomes a blob — the class of entry that ranked
#: badly for three separate measured reasons in October and could not be fixed by any ranking lever.
#: Enforced at the CAPTURE surface (`sigil write`, MCP write), not in `write_entry`: an importer migrating
#: 800 historical notes must not be blocked by a rule about new ones.
SUMMARY_LIMIT_BYTES = 280


class SummaryTooLong(ValueError):
    def __init__(self, summary: str, limit: int = SUMMARY_LIMIT_BYTES):
        self.size = len(summary.encode("utf-8"))
        self.limit = limit
        super().__init__(
            f"summary is {self.size} bytes, limit {limit}: this is a blob, not a handle. "
            f"Put the detail in --body and keep one sentence here "
            f"(the first {limit} bytes are: {summary.encode('utf-8')[:limit].decode('utf-8', 'ignore')!r}…)")


#: A summary that opens with one of these is not a statement, it is a serialised object or a fenced
#: block that was pasted where the handle belongs (#77).
_BLOB_OPENERS = ("{", "[", "```", "~~~")


def looks_like_blob(summary: str) -> bool:
    """Is this summary a serialised object or a fenced block rather than prose? (#77)

    The 0.3.0 importer and `capture` both accepted a summary of this shape, and a blob's tokens
    index as well as prose's — so twenty-one entries on the live vault were ranking on text no agent
    can read, inside a 400-token recall budget. The body may keep the blob; the handle may not.
    """
    s = (summary or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s.startswith(_BLOB_OPENERS)


#: A line needs this many words before it reads as a statement rather than a label. "Heading" is a
#: title, "Release notes" is a topic; neither is a fact an agent can act on.
_PROSE_MIN_WORDS = 4


def first_prose_line(text: str, *, limit: int = 200) -> str:
    """The first line of `text` that can serve as a retrieval handle (#77), or "".

    Skips blank lines, headings, fences, and anything that is a serialised blob. A candidate needs a
    sentence's worth of words, because a title or a label is not a fact: deriving a summary from the
    body is only an improvement if the derived line can be read as a statement.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or looks_like_blob(line):
            continue
        if len(line.split()) < _PROSE_MIN_WORDS or not _PROSE_RE.search(line):
            continue
        return line[:limit]
    return ""


class SummaryNotProse(ValueError):
    """A summary that cannot serve as a retrieval handle because it is a serialised blob."""

    def __init__(self, summary: str):
        self.head = (summary or "").strip()[:80]
        super().__init__(
            f"summary starts with {self.head!r}, which is a serialised blob, not a statement: the "
            "summary is the retrieval handle and its tokens rank, so a JSON body or a fenced block "
            "crowds real recall. Write one sentence here and put the blob in --body "
            "(or pass --allow-summary-blob if this really is the handle).")


def check_summary(summary: str, limit: int = SUMMARY_LIMIT_BYTES, allow_long: bool = False,
                  allow_blob: bool = False) -> None:
    """Raise unless the summary can serve as a retrieval handle.

    `allow_long` and `allow_blob` are deliberate opt-outs for a caller that knows what it is doing;
    an importer migrating a historical corpus uses neither, it reports instead.
    """
    if not allow_blob and looks_like_blob(summary):
        raise SummaryNotProse(summary)
    if allow_long:
        return
    if len((summary or "").encode("utf-8")) > limit:
        raise SummaryTooLong(summary, limit)


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # A digit-only STRING must be quoted or it comes back as an int: an id of "04159069" round-tripped
    # to "4159069" (CI, 3.13) — leading zero gone, which silently breaks every supersedes reference
    # that names it. Same for a project called "2026" and anything else that looks numeric. The
    # quote guard and the parser share NUMBER_RE, so they cannot drift apart again.
    if s == "" or NUMBER_RE.match(s) or s in ("true", "false") or any(c in s for c in ':#\n"\''):
        return json.dumps(s, ensure_ascii=False)
    return s


def _parse_scalar(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if raw[0] == raw[-1] and raw[0] in "\"'" and len(raw) >= 2:
        # a quoted value is ALWAYS a string, whatever it looks like
        try:
            return json.loads(raw) if raw[0] == '"' else raw[1:-1]
        except json.JSONDecodeError:
            return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    if NUMBER_RE.match(raw):
        return float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)
    return raw


#: The sections this writer generates from the frontmatter. They are stripped from the body
#: before it is written back, so re-writing an entry - which happens every time a closure is
#: recorded - does not duplicate them.
GENERATED_SECTIONS = ("## Supersedes", "## Edges", "## Edge closures")


def strip_generated(body: str) -> str:
    """Drop the generated sections (and the bullets under them) from a body."""
    out: list[str] = []
    skipping = False
    for line in (body or "").splitlines():
        if line.strip() in GENERATED_SECTIONS:
            skipping = True
            continue
        if skipping:
            if line.startswith("#"):
                skipping = False
            else:
                continue
        out.append(line)
    return "\n".join(out).strip()


@dataclass
class Entry:
    id: str = ""
    kind: str = "conclusion"
    state: str = "raw"
    agent: str = ""
    project: str = ""
    session: str = ""
    created: str = ""
    updated: str = ""
    confidence: float = 0.5
    importance: float = 0.0
    source: str = ""
    model: str = ""
    evidence_kind: str = ""
    ask: str = ""
    summary: str = ""
    evidence: str = ""
    supersedes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    #: The graph edges this entry asserted, as "SRC:PRED:DST" specs. The graph lives here, in the
    #: markdown: a db that is deleted can be rebuilt from these (#24).
    edges: list[str] = field(default_factory=list)
    #: The windows this entry's edges closed: [{"spec": "SRC:PRED:DST", "at": "<instant>"}]. A
    #: rebuild has to restore closed windows, not live ones - a resurrected belief is worse than a
    #: lost one.
    edge_closures: list[dict] = field(default_factory=list)
    body: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_id()
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {self.state!r}")
        if not self.created:
            self.created = now_iso()
        if not self.updated:
            self.updated = self.created

    # --- serialisation -------------------------------------------------
    def to_markdown(self) -> str:
        head = [
            f"id: {_scalar(self.id)}",
            f"kind: {self.kind}",
            f"state: {self.state}",
            f"agent: {_scalar(self.agent)}",
            f"project: {_scalar(self.project)}",
            f"session: {_scalar(self.session)}",
            f"created: {self.created}",
            f"updated: {self.updated}",
            f"confidence: {self.confidence}",
            f"importance: {self.importance}",
            f"source: {_scalar(self.source)}",
        ]
        # which model wrote this. It exists only at write time — a quant swap or a provider change
        # leaves every older entry looking like it came from the same mind (#110).
        if self.model:
            head.append(f"model: {_scalar(self.model)}")
        # summary and evidence belong in the frontmatter, not only in the body: the body copy is for
        # humans, while from_markdown/exports read the fields. Writing them body-only silently lost
        # evidence on every round-trip (which disarmed the promotion gate and the DPO reason).
        head.append(f"summary: {_scalar(self.summary)}")
        if self.evidence:
            head.append(f"evidence: {_scalar(self.evidence)}")
        if self.evidence_kind:
            head.append(f"evidence_kind: {self.evidence_kind}")
        if self.ask:
            head.append(f"ask: {_scalar(self.ask)}")
        if self.supersedes:
            head.append("supersedes: " + json.dumps(self.supersedes, ensure_ascii=False))
        if self.tags:
            head.append("tags: " + json.dumps(self.tags, ensure_ascii=False))
        if self.edges:
            head.append("edges: " + json.dumps(self.edges, ensure_ascii=False))
        if self.edge_closures:
            head.append("edge_closures: " + json.dumps(self.edge_closures, ensure_ascii=False))
        title = self.summary or self.body.splitlines()[0] if (self.summary or self.body) else self.id
        parts = ["---"] + head + ["---", "", f"# {title}", ""]
        if self.summary:
            parts += [self.summary, ""]
        if self.evidence:
            parts += ["**Evidence:** " + self.evidence, ""]
        # the H1/summary/evidence preamble is written from the fields above, so the body keeps
        # only what the writer wrote. Re-writing an entry happens every time an edge or a closure
        # is recorded into it, and a body that grew a copy per rewrite was unreadable within a
        # dozen writes.
        body = strip_generated(self.stripped_body())
        if body:
            parts += [body, ""]
        if self.supersedes:
            parts += ["## Supersedes", ""] + [f"- [[{s}]]" for s in self.supersedes] + [""]
        if self.edges:
            parts += ["## Edges", ""] + [f"- {spec}" for spec in self.edges] + [""]
        if self.edge_closures:
            parts += ["## Edge closures", ""] + [
                f"- {c.get('spec', '')} closed at {c.get('at', '')}" for c in self.edge_closures] + [""]
        return "\n".join(parts)

    @classmethod
    def from_markdown(cls, text: str) -> Entry:
        fm: dict = {}
        body = text
        m = FM_RE.match(text)
        if m:
            body = text[m.end():]
            for line in m.group(1).splitlines():
                mm = KV_RE.match(line)
                if mm:
                    fm[mm.group(1)] = _parse_scalar(mm.group(2))
        title = ""
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        tags = fm.get("tags") or []
        sup = fm.get("supersedes") or []
        edges = fm.get("edges") or []
        closures = fm.get("edge_closures") or []
        if not isinstance(edges, list):
            edges = [edges]
        if not isinstance(closures, list):
            closures = [closures]
        return cls(
            id=str(fm.get("id") or new_id()),
            kind=str(fm.get("kind") or "conclusion"),
            state=str(fm.get("state") or "raw"),
            agent=str(fm.get("agent") or ""),
            project=str(fm.get("project") or ""),
            session=str(fm.get("session") or ""),
            created=str(fm.get("created") or ""),
            updated=str(fm.get("updated") or fm.get("created") or ""),
            confidence=float(fm.get("confidence") or 0.5),
            importance=float(fm.get("importance") or 0.0),
            source=str(fm.get("source") or ""),
            model=str(fm.get("model") or ""),
            ask=str(fm.get("ask") or ""),
            summary=str(fm.get("summary") or title),
            evidence=str(fm.get("evidence") or ""),
            evidence_kind=str(fm.get("evidence_kind") or ""),
            supersedes=list(sup) if isinstance(sup, list) else [str(sup)],
            tags=list(tags) if isinstance(tags, list) else [str(tags)],
            edges=[str(s) for s in edges],
            edge_closures=[c for c in closures if isinstance(c, dict)],
            body=body.strip(),
        )

    def stripped_body(self) -> str:
        """Body without the duplicated H1/summary preamble (what a reader wants as content)."""
        lines = self.body.splitlines()
        out: list[str] = []
        for line in lines:
            if line.startswith("# ") and not out:
                continue
            if line.strip() == self.summary.strip():
                continue
            if line.startswith("**Evidence:**"):
                continue
            out.append(line)
        return "\n".join(out).strip()


def entry_path(root: Path, eid: str, summary: str = "") -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", (summary or "").lower()).strip("-")[:48].rstrip("-")
    return Path(root) / "notes" / f"{eid}-{slug}.md" if slug else Path(root) / "notes" / f"{eid}.md"


def write_entry(root: str | Path, entry: Entry) -> Path:
    path = entry_path(Path(root), entry.id, entry.summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry.to_markdown(), encoding="utf-8")
    return path


def read_entry(path: str | Path) -> Entry:
    return Entry.from_markdown(Path(path).read_text(encoding="utf-8", errors="replace"))


def iter_entries(root: str | Path):
    notes = Path(root) / "notes"
    if not notes.exists():
        return
    for p in sorted(notes.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if not text.lstrip().startswith("---"):
                continue  # not a sigil entry (hand-written note, README, ...)
            yield p, Entry.from_markdown(text)
        except (ValueError, OSError):
            continue


def find_entry(root: str | Path, eid: str) -> Path | None:
    if not eid:
        return None
    for p in (Path(root) / "notes").rglob(f"{eid}*.md"):
        return p
    return None


#: The edge spec format the markdown carries: "SRC:PRED:DST". One definition, so the writer,
#: the reader, the CLI and the graph cannot drift apart on how a spec is written or split.
def format_edge_spec(src: str, predicate: str, dst: str) -> str:
    return f"{src}:{predicate}:{dst}"


def parse_edge_spec(spec: str) -> tuple[str, str, str]:
    """Split "SRC:PRED:DST" into its three parts. Raises ValueError on a malformed spec.

    Exactly three parts. `split(":", 2)` used to absorb every extra colon into `dst`, so
    `wraith:kernel-7.0.0-43:required:nvidia-driver-615-open` parsed "fine" as src=wraith,
    predicate=kernel-7.0.0-43, dst="required:nvidia-driver-615-open": a claim nobody wrote, and one
    no writer can re-emit, since `format_edge_spec` would put those back as four parts (#98). An
    entity name containing a colon is not expressible in this format — that is the format's rule,
    and it is worth saying out loud rather than reshaping silently.
    """
    parts = (spec or "").split(":")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise ValueError(f"edge spec takes exactly three colon-separated parts "
                         f"(src:predicate:dst), got {len(parts)} in {spec!r}")
    src, predicate, dst = (part.strip() for part in parts)
    return src, predicate, dst
