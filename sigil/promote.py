"""The promotion ladder: raw capture -> reflection -> promoted memory.

The reflection pass runs as its own isolated step (its own process / its own context), reads a
session transcript or a batch of raw captures, and proposes distilled entries. Proposals carry an
importance score and must cite evidence; below the threshold, or with no evidence, they stay raw.
That gate decision is the training label — see export.py.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import KINDS
from . import evidence as evidence_mod
from .config import DEFAULT_LLM_TIMEOUT, Config
from .graph import add_edge, close_superseded, ensure_entity
from .index import connect
from .store import (
    Entry,
    check_summary,
    first_prose_line,
    format_edge_spec,
    iter_entries,
    looks_like_blob,
    write_entry,
)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "promote.md"

#: The roles a Hermes session row can carry that belong in a transcript. A `tool` row is included
#: because a tool result usually carries the evidence the reflection pass needs.
TURN_ROLES = ("user", "assistant", "system", "tool", "human")

#: A line that opens a turn: `role: text`. The loader emits exactly this shape, and the chunker
#: splits on it, so a chunk boundary is always a turn boundary.
_TURN_LINE = re.compile(r"^(user|assistant|system|tool|human):\s", re.I)


@dataclass
class LoadStats:
    """What the last load did with the file it was given.

    A corrupt line is skipped, never fatal — but it is counted, because a session that quietly lost
    half its lines is a session that lies.
    """
    messages: int = 0
    malformed: int = 0
    skipped: int = 0

    def reset(self) -> None:
        self.messages = 0
        self.malformed = 0
        self.skipped = 0


#: The counts from the most recent `load_transcript` call.
LAST_LOAD_STATS = LoadStats()


def load_transcript(source: str | Path, stats: LoadStats | None = None) -> str:
    """Read a session file — or a transcript already in hand — into plain `role: text` lines (#2).

    Two shapes arrive in practice and they want different treatment:

    * **Hermes session JSONL** (`.jsonl`, or content that sniffs as one): one JSON object per line
      with `role` and `content`, where `content` is a string or a list of `{"type": "text",
      "text": ...}` parts. Rendered as `role: text`, parts of one message joined with a single space.
    * **Anything else** (markdown, a pasted transcript): passed through untouched, because a human
      already chose its shape.

    A row that is not a message (`type: meta`), a malformed line, and a message with no text are
    skipped rather than fatal: a session file with one corrupt line is still a session. An empty or
    missing file IS fatal — a pass over nothing must not look like a pass.

    What was skipped is counted: in `stats` when the caller passes one, otherwise in the module's
    `LAST_LOAD_STATS` — exactly one of the two, never silently neither.
    """
    counts = stats if stats is not None else LAST_LOAD_STATS
    counts.reset()
    if isinstance(source, Path):
        path: Path | None = source
        text = ""
    else:
        raw = str(source)
        looks_like_a_path = "\n" not in raw and len(raw) < 4096
        path = Path(raw) if looks_like_a_path else None
        text = raw if path is None else ""
    if path is not None:
        if not path.exists():
            raise ValueError(f"transcript not found: {path}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ValueError(f"cannot read transcript {path}: {exc}") from exc
    if not text.strip():
        raise ValueError(f"empty transcript: {path or '<text>'}")

    if not _sniffs_jsonl(text, path):
        return text
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            counts.malformed += 1
            continue  # one corrupt line is not a broken session
        if not isinstance(row, dict):
            counts.skipped += 1
            continue
        role = str(row.get("role") or "").lower()
        if role not in TURN_ROLES:
            counts.skipped += 1
            continue  # meta/control rows are not conversation
        content = row.get("content")
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict)
                     and p.get("type", "text") in ("text", "input_text", "output_text")]
        else:
            counts.skipped += 1
            continue
        message = re.sub(r"\s+", " ", " ".join(str(p) for p in parts if p)).strip()
        if not message:
            counts.skipped += 1
            continue
        lines.append(f"{role}: {message}")
        counts.messages += 1
    if not lines:
        raise ValueError(f"no messages in transcript: {path or '<text>'}")
    return "\n".join(lines)


def read_session_db(db_path: str | Path, session_id: str, include_tools: bool = False,
                    tool_chars: int = 2000, stats: LoadStats | None = None) -> str:
    """Render a Hermes session from its `state.db` as `role: text` lines.

    Hermes stopped writing `profiles/<p>/sessions/<id>.jsonl` for live sessions — the transcripts a
    reflection pass needs now live in `state.db`'s `messages` table, and a hook pointed at the JSONL
    directory silently reflects nothing from that day on. This reads the same turns from the db, in
    the SAME shape `load_transcript` emits, so the chunker and the prompt cannot tell them apart.

    Prose by default: `user` and `assistant` rows only. A state db holds everything — 376 KB of tool
    output against 18 KB of prose on a measured session — and it is the prose that carries decisions,
    measurements and corrections. `include_tools` adds `tool <name>: …` lines capped at `tool_chars`,
    marked where they were cut, because a truncated tool result is still readable evidence.

    An assistant row with no content is a tool-call turn, not a silent turn: it is skipped and
    counted, never rendered as an empty `assistant:`. A session with no prose at all raises — a pass
    over nothing must not look like a pass.
    """
    import sqlite3

    counts = stats if stats is not None else LAST_LOAD_STATS
    counts.reset()
    path = Path(db_path).expanduser()
    if not path.exists():
        raise ValueError(f"session db not found: {path}")
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("--session-db needs --session <id>: the db holds many sessions")
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            # an exact id, or a prefix that matches exactly one session — a 64-hex id is not something
            # a human types, and the same rule as `find_entry` keeps the CLI usable without guessing
            matches = [r["id"] for r in c.execute(
                "SELECT id FROM sessions ORDER BY coalesce(last_activity_at, started_at) DESC")
                if r["id"] == sid or sid in r["id"]]
            if len(matches) != 1:
                why = ("no session matches" if not matches
                       else "is ambiguous between " + ", ".join(matches[:3]))
                raise ValueError(f"{sid!r} {why} in {path}")
            sid = matches[0]
            rows = c.execute("SELECT role, content, tool_name FROM messages WHERE session_id = ? "
                             "ORDER BY timestamp, id", (sid,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error as exc:
        raise ValueError(f"{path} is not a readable Hermes session db: {exc}") from exc

    lines: list[str] = []
    for row in rows:
        role = str(row["role"] or "").lower()
        body = re.sub(r"\s+", " ", str(row["content"] or "")).strip()
        if role in ("user", "assistant", "human"):
            if not body:
                counts.skipped += 1      # a tool-call turn: the call is in the row, not the prose
                continue
            lines.append(f"user: {body}" if role == "human" else f"{role}: {body}")
            counts.messages += 1
        elif role == "tool" and include_tools:
            if not body:
                counts.skipped += 1
                continue
            if tool_chars > 0 and len(body) > tool_chars:
                body = body[:tool_chars] + " …[truncated]"
            name = str(row["tool_name"] or "").strip()
            lines.append(f"tool {name}: {body}" if name else f"tool: {body}")
            counts.messages += 1
        else:
            counts.skipped += 1
    if not lines:
        raise ValueError(f"session {sid!r} has no user or assistant turns")
    return "\n".join(lines)


def _sniffs_jsonl(text: str, path: Path | None) -> bool:
    """A `.jsonl` name is a promise; without one, the first non-empty line has to be a message row."""
    if path is not None and path.suffix.lower() in (".jsonl", ".ndjson"):
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            return False
        try:
            row = json.loads(line)
        except ValueError:
            return False
        return isinstance(row, dict) and "role" in row and "content" in row
    return False


def chunk_transcript(text: str, max_chars: int = 24000) -> list[str]:
    """Split a transcript on TURN boundaries so no message is ever cut in half (#2).

    A chunk is a whole number of turns, packed greedily up to `max_chars`. One turn longer than the
    cap is returned **whole** as its own chunk: truncating a message silently is exactly the failure
    this project exists to stop, so bounding an oversized turn is the caller's explicit decision, not
    the chunker's quiet one. `max_chars <= 0` disables chunking.

    Markdown without `role:` lines is split on blank lines instead — still a boundary a human drew.
    Joining the returned chunks with the text's own separator reproduces the input exactly.
    """
    if max_chars <= 0:
        return [text]
    if len(text) <= max_chars:
        return [text]

    lines = text.split("\n")
    blocks: list[list[str]] = []
    if sum(1 for line in lines if _TURN_LINE.match(line)) >= 1:
        for line in lines:
            if not blocks or _TURN_LINE.match(line):
                blocks.append([line])
            else:
                blocks[-1].append(line)   # blank lines and continuations stay with their turn
    else:
        for line in lines:                # paragraphs; a blank run closes the block before it
            if blocks and line.strip() and blocks[-1][-1].strip() == "":
                blocks.append([line])
            elif blocks:
                blocks[-1].append(line)
            else:
                blocks.append([line])

    chunks: list[str] = []
    current: list[list[str]] = []
    size = 0
    for block in blocks:
        block_size = len("\n".join(block))
        extra = block_size + (1 if current else 0)
        if current and size + extra > max_chars:
            chunks.append("\n".join("\n".join(b) for b in current))
            current, size = [block], block_size
        else:
            current.append(block)
            size += extra
    if current:
        chunks.append("\n".join("\n".join(b) for b in current))
    return [c for c in chunks] or [text]


@dataclass
class Proposal:
    kind: str
    summary: str
    body: str = ""
    evidence: str = ""
    importance: float = 0.0
    confidence: float = 0.5
    ask: str = ""
    entities: list[tuple[str, str]] = field(default_factory=list)   # [(name, kind)]
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # [(src, predicate, dst)]
    supersedes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict) -> Proposal:
        kind = str(d.get("kind", "conclusion"))
        if kind not in KINDS:
            kind = "conclusion"
        ents = [(str(e.get("name")), str(e.get("kind", "thing")))
                for e in d.get("entities", []) if isinstance(e, dict) and e.get("name")]
        edges = [(str(e.get("src")), str(e.get("predicate")), str(e.get("dst")))
                 for e in d.get("edges", []) if isinstance(e, dict) and e.get("src") and e.get("dst")]
        return cls(
            kind=kind,
            summary=str(d.get("summary") or "").strip(),
            body=str(d.get("body") or "").strip(),
            evidence=str(d.get("evidence") or "").strip(),
            importance=float(d.get("importance") or 0.0),
            confidence=float(d.get("confidence") or 0.5),
            ask=str(d.get("ask") or "").strip(),
            entities=ents, edges=edges,
            supersedes=[str(s) for s in d.get("supersedes", [])],
            tags=[str(t) for t in d.get("tags", [])],
        )


class FakeProvider:
    """Deterministic provider for tests and dry runs."""

    def __init__(self, payload: list[dict] | str):
        self.payload = payload

    def complete(self, prompt: str) -> str:  # noqa: ARG002
        return self.payload if isinstance(self.payload, str) else json.dumps({"entries": self.payload})


class HttpProvider:
    """Any OpenAI-compatible /chat/completions endpoint (Ollama Cloud, llama.cpp, vLLM, Ollama)."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: int = DEFAULT_LLM_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "stream": False,
        }).encode()
        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:  # a status code, not a transport failure
            raise RuntimeError(f"LLM request failed: {url}: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {url}: {exc}") from exc
        except OSError as exc:  # a socket timeout surfaces here, not as a URLError
            if isinstance(exc, TimeoutError):  # includes socket.timeout
                # the timeout is the ONE failure with a knob behind it, and the old message named the
                # endpoint instead, so a user read "timed out" and had no idea what to change
                raise RuntimeError(
                    f"LLM request failed: {url}: no response within {self.timeout}s. One chunk is one "
                    f"request, so a large chunk needs a longer budget: raise `--endpoint-timeout` (or "
                    f"`[llm] timeout` in .sigil/config.toml), lower `--max-chars`, or point "
                    f"`[llm] base_url` at a faster endpoint") from exc
            raise RuntimeError(f"LLM request failed: {url}: {type(exc).__name__}: {exc}") from exc
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LLM request failed: {url}: bad response: {exc}") from exc


def build_prompt(transcript: str, cfg: Config, existing: list[str] | None = None) -> str:
    base = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else _DEFAULT_PROMPT
    ents = ", ".join(cfg.predicates)
    known = "\n".join(f"- {e}" for e in (existing or [])[:40]) or "- (none)"
    return (base.replace("{{PREDICATES}}", ents)
                .replace("{{KNOWN_ENTRIES}}", known)
                .replace("{{TRANSCRIPT}}", transcript.strip()))


_DEFAULT_PROMPT = """You are the reflection pass of an agent memory system.
Read the transcript and extract only what a future session would need: decisions with their reason,
lessons, verified facts, handoffs. Not chatter, not narration, not a summary of the conversation.
Return JSON: {"entries":[{"kind":"decision|lesson|fact|handoff|conclusion","summary":"one statement",
"body":"detail","evidence":"the concrete proof: number, path, command, quote","importance":0.0-1.0,
"ask":"the user request that led here","entities":[{"name":"x","kind":"project|file|tool|person|concept"}],
"edges":[{"src":"x","predicate":"one of {{PREDICATES}}","dst":"y"}],"supersedes":["entry-id"]}]}
Known entries: {{KNOWN_ENTRIES}}
TRANSCRIPT:
{{TRANSCRIPT}}"""


def parse_response(raw: str) -> list[Proposal]:
    """Tolerate prose around the JSON object — models leak fences no matter what you ask."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON object in model response")
    # strict=False: a small model puts a literal newline or tab inside a JSON string, and strict
    # `json.loads` refuses the whole response — 2 of 27 live sessions failed exactly that way (#105).
    # The characters are legal in the VALUE we want; only the framing needs to be JSON.
    data = json.loads(m.group(0), strict=False)
    items = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("response has no entries list")
    return [Proposal.from_json(d) for d in items if isinstance(d, dict) and d.get("summary")]


def claim_key(summary: str) -> str:
    """The claim's identity: its summary with whitespace collapsed and case folded.

    One definition, because two callers decide "is this the same claim": `dedupe_proposals` within a
    batch, and the hook across chunks now that each chunk is applied as it finishes (#111).
    """
    return " ".join(str(summary).split()).lower()


def dedupe_proposals(proposals: list) -> list:
    """Union per-chunk proposals, dropping a summary an earlier chunk already produced.

    Chunking sends neighbouring context to the same model, so one claim can come back twice — and a
    chunked pass that writes it twice turns a clean corpus into a pile of near-duplicates. The key is
    the summary with whitespace collapsed and case folded: the reflection pass writes that summary
    itself, so two spellings of one claim are one claim.
    """
    seen: set[str] = set()
    kept = []
    for proposal in proposals:
        key = claim_key(proposal.summary)
        if key in seen:
            continue
        seen.add(key)
        kept.append(proposal)
    return kept


def reflect(transcript: str, cfg: Config, provider, existing: list[str] | None = None) -> list[Proposal]:
    return parse_response(provider.complete(build_prompt(transcript, cfg, existing)))


def _prose_handle(summary: str, body: str) -> str:
    """The summary as a usable handle: quotes stripped, a blob replaced by the body's first statement.

    A proposal is a model's guess at what the transcript said, so the field can arrive in any shape.
    Losing the entry over a mis-shaped handle would be the wrong trade — the body is right there and
    the statement can be recovered from it — but writing a blob or a quoted-and-truncated scalar means
    the entry ranks on text nobody can read, which is what #77 and #54 are about.
    """
    text = (summary or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if looks_like_blob(text):
        derived = first_prose_line(body)
        if derived:
            return derived
    return text


def apply(proposals: list[Proposal], cfg: Config, agent: str = "", project: str = "",
          session: str = "", source: str = "", report: dict | None = None,
          model: str = "") -> list[Entry]:
    """Write proposals, gated by the promotion threshold.

    promoted    importance >= cfg.promote_threshold AND evidence present
    raw         everything else (kept verbatim for a later pass; it costs nothing to keep)

    A model's proposal is a CALLER like any other, so its edges face the closed vocabulary here,
    at the write boundary (#103). The first real pass over a live session proposed
    `main:implemented_by:sigil`; the spec went into the markdown, the graph refused it, and `audit`
    reported it from then on — the claim was stored where it could never be queried. A refused spec
    is now reported in `report["skipped_edges"]` and written nowhere: the edge is the claim, the
    entry is the memory, and only the claim was bad.
    """
    written: list[Entry] = []
    refused: list[str] = []
    refused_supersedes: list[str] = []
    # a model writes an ID field the same way it writes a carrier: with prose in it. Measured on a
    # real pass: `supersedes: "Re-measured after engine 0.1.56, llama.cpp e853ec9a8 …"` — the pair
    # then references nothing, `audit` reports it forever and the preference export silently loses it.
    known_ids = {e.id for _pth, e in iter_entries(cfg.root)}
    for p in proposals:
        # the model is a caller like any other, so the handle rule applies here too (#77, #85): a
        # measured run over a real transcript emitted a summary starting with a quote, and this path
        # wrote it straight through because only the CLI and the importer checked the shape.
        p.summary = _prose_handle(p.summary, p.body)
        promoted = p.importance >= cfg.promote_threshold and bool(p.evidence)
        # the entry that asserted an edge records it, so the graph survives a lost db (#24).
        # A raw proposal never enters the graph, so it carries no edges either.
        kept_supersedes: list[str] = []
        for old_id in p.supersedes:
            if old_id in known_ids:
                kept_supersedes.append(old_id)
            else:
                refused_supersedes.append(old_id)
        specs: list[str] = []
        storable: list[tuple[str, str, str]] = []
        if promoted:
            for src, predicate, dst in p.edges:
                # a part holding the separator cannot round-trip: "a:b:c:d" re-parses as four parts,
                # so the claim would be stored where the replay refuses it (#105, measured live as
                # GraphRAG:requires:enable_thinking:false)
                if any(":" in part for part in (src, predicate, dst)):
                    refused.append(format_edge_spec(src, predicate, dst))
                    continue
                if predicate not in cfg.predicates:
                    refused.append(format_edge_spec(src, predicate, dst))
                    continue
                specs.append(format_edge_spec(src, predicate, dst))
                storable.append((src, predicate, dst))
        e = Entry(
            kind=p.kind, state="promoted" if promoted else "raw", agent=agent, project=project,
            session=session, model=model,
            # derived, never asked of the model: a declared kind would be another unverifiable claim,
            # while the evidence text itself is right there (#109)
            evidence_kind=evidence_mod.classify(p.evidence),
            confidence=p.confidence, importance=p.importance,
            source=source or "reflect", ask=p.ask, summary=p.summary, evidence=p.evidence,
            supersedes=kept_supersedes, tags=p.tags, edges=specs, body=p.body,
        )
        write_entry(cfg.root, e)
        written.append(e)
        # graph only learns from what survived the gate: a raw capture is allowed to be wrong
        if promoted:
            for name, kind in p.entities:
                c = connect(cfg.db_path)
                with c:
                    ensure_entity(c, name, kind)
                c.close()
            # Assert exactly what the frontmatter kept. This loop used to walk `p.edges` again, so a
            # spec refused by the gate above still reached `add_edge`, which records the spec into the
            # entry's markdown (#112). Measured: entries 3d922801 and cb000190 carried
            # `vector:implements:gpt-oss:120b` in the markdown and `gpt-oss-120b` in the graph — the
            # same claim in two shapes, one of which no replay can parse.
            for src, predicate, dst in storable:
                try:
                    # the entry's `created` is the window's start, so a rebuild reproduces it
                    add_edge(cfg, src, predicate, dst, note_id=e.id, at=e.created)
                except ValueError:
                    continue  # predicate outside the closed vocabulary: skip, don't poison the graph
        # a correction proposed by the reflection pass closes the belief it corrects (#20), the same
        # way `sigil write --supersedes` does; an edge the new entry restates is left live
        if e.supersedes:
            close_superseded(cfg, e.supersedes, e)
    if report is not None:
        report["skipped_edges"] = refused
        report["skipped_supersedes"] = refused_supersedes
    return written


def record_capture(cfg: Config, text: str, agent: str = "", project: str = "", session: str = "",
                   ask: str = "", source: str = "", allow_blob: bool = False) -> Entry:
    """Store a raw capture. Cheap, unfiltered, one fact per call.

    The summary is the first prose line rather than blindly the first line: a capture whose body
    opens with a JSON blob used to hand that blob to the retrieval handle, which then ranked on
    text no agent can use (#77). A body with no prose at all is refused by `check_summary` unless
    the caller opted out — the entry is not worth keeping if nothing in it can be stated.
    """
    first = text.splitlines()[0][:200]
    summary = first if allow_blob else (first_prose_line(text) or first)
    check_summary(summary, allow_blob=allow_blob)
    e = Entry(kind="capture", state="raw", agent=agent, project=project, session=session,
              ask=ask, summary=summary, body=text, source=source or "capture")
    write_entry(cfg.root, e)
    return e
