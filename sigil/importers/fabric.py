"""Fabric (Icarus) corpus -> sigil entries.

A fabric entry is a markdown file with YAML-ish frontmatter::

    ---
    id: "a86b8c2a"
    agent: "cipher"
    platform: "cli"
    timestamp: "2026-10-10T18:23:30Z"
    type: "code-session"
    tier: "hot"
    summary: "one line, the retrieval signal"
    project_id: "shitter"
    status: "completed"
    outcome: "what actually happened"
    verified: "true"
    evidence: "the exact command and its output"
    training_value: "high"        # high | normal | low
    tags: [shitter, migration]
    ---

    the body

Mapping onto a sigil entry:

    type            -> kind     decision/lesson/fact/handoff/conclusion/question keep
                               their name, session/code-session become `capture`,
                               anything else becomes `conclusion`
    training_value  -> state    high|normal -> `promoted`, low -> `pruned`,
                               missing -> `raw`
    training_value  -> importance  high 0.9, normal 0.7, low 0.3, missing 0.0
    verified        -> confidence  true 1.0, false 0.0, absent 0.5
    summary         -> summary
    outcome + evidence -> evidence
    project_id      -> project
    agent           -> agent
    session_id      -> session
    timestamp       -> created
    id              -> entry id (a stable hash of the source path when absent)
    tags            -> tags, plus a reason tag on pruned entries
    body            -> body, minus the duplicated H1/summary preamble

Session noise: `session`/`code-session` entries with `training_value: low` are the corpus's
"fixed a bug" noise. They are never written as promoted -- they are skipped entirely unless
`include_noise=True`, which writes them as `pruned`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import DEFAULT_PROMOTE_THRESHOLD
from ..store import Entry, first_prose_line, looks_like_blob, looks_like_transcript, write_entry

#: fabric `type` -> sigil `kind`; unknown types fall back to DEFAULT_KIND
KIND_BY_TYPE = {
    "decision": "decision",
    "lesson": "lesson",
    "fact": "fact",
    "handoff": "handoff",
    "conclusion": "conclusion",
    "question": "question",
    "session": "capture",
    "code-session": "capture",
}
DEFAULT_KIND = "conclusion"

#: fabric `training_value` -> sigil `state`; missing or unknown values fall back to DEFAULT_STATE
STATE_BY_TRAINING_VALUE = {"high": "promoted", "normal": "promoted", "low": "pruned"}
DEFAULT_STATE = "raw"

#: fabric types whose low-value entries are corpus noise, not memory
NOISE_TYPES = frozenset({"session", "code-session"})

#: fabric `training_value` -> store `importance`, so a promoted entry clears sigil's own
#: promotion threshold (cfg.promote_threshold, 0.5) and a pruned one does not
IMPORTANCE_BY_TRAINING_VALUE = {"high": 0.9, "normal": 0.7, "low": 0.3}

#: fabric `verified` -> store `confidence`; absent keeps the store default (0.5)
CONFIDENCE_BY_VERIFIED = {"true": 1.0, "false": 0.0}

#: files that sit in a corpus root but are documentation, not entries
IGNORED_NAMES = frozenset({"README.md", "FRONTMATTER-TEMPLATE.md"})

#: why an entry landed in the pruned state (a tag, never a supersedes edge)
PRUNED_TAG = "pruned:low-training-value"

#: an entry that claimed `promoted` without evidence was demoted to raw (#26)
NO_EVIDENCE_TAG = "import:no-evidence"

#: an entry whose summary is a pasted tool transcript, not a claim (#30)
TRANSCRIPT_TAG = "import:transcript-dump"

_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


@dataclass
class ImportResult:
    """What an import did: the entries it wrote, the noise it skipped, the counts.

    `skipped` carries a reason with every path. A bare integer could not tell "noise we meant to
    drop" from "we could not parse this file", and the difference is the whole point of the report
    (#17). `directories_ignored` is the other half: the importer reads the top level only, so the
    subdirectories it walked past are named with the number of markdown files each holds.
    """

    written: list[Entry] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    directories_ignored: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_state: dict[str, int] = field(default_factory=dict)


#: The fixed vocabulary of skip reasons. The report is consumed by people and by scripts, so the
#: strings are stable and exhaustive rather than free-form prose at each call site.
SKIP_IGNORED_NAME = "ignored name"
SKIP_EMPTY_BODY = "empty body"
SKIP_NO_TYPE = "no type"
SKIP_UNPARSABLE = "unparsable frontmatter"
SKIP_NOISE = "noise (training_value: low)"
SKIP_SUMMARY_NOT_PROSE = "summary not prose"

#: Subdirectories a fabric corpus keeps scaffolding in. `--recurse` walks past the top level, and
#: these are the trees it must still refuse: stubs, duplicates, hub notes and templates are not
#: entries, and importing 4,635 of them would bury the corpus that is (#17).
DEFAULT_SKIP_DIRS = frozenset({
    "_dupes", "_stubs", "_meta", "templates", ".obsidian", "assets",
})


def import_fabric(src_dir: str | Path, vault_root: str | Path, *,
                  dry_run: bool = False, include_noise: bool = False,
                  promote_threshold: float = DEFAULT_PROMOTE_THRESHOLD,
                  recurse: bool = False,
                  skip_dirs: frozenset[str] | set[str] | None = None) -> ImportResult:
    """Convert a fabric corpus into sigil entries under `vault_root`.

    Only the files directly in `src_dir` are considered by default -- a corpus keeps its stubs and
    duplicates in sibling directories, and those are not what an import is for. `recurse=True`
    walks the whole tree instead and refuses `skip_dirs` (defaulting to `DEFAULT_SKIP_DIRS`), so a
    corpus with a different shape can be imported without a code change.

    `promote_threshold` is sigil's own promotion gate: an entry is promoted only when its
    importance clears it AND it carries evidence - the same gate promote.py applies. Without it
    509 of 780 imported entries claimed `promoted` while having no evidence at all (#26).
    """
    src = Path(src_dir).expanduser().resolve()
    root = Path(vault_root).expanduser().resolve()
    if not src.is_dir():
        raise NotADirectoryError(f"not a directory: {src}")

    skip = DEFAULT_SKIP_DIRS if skip_dirs is None else frozenset(skip_dirs)
    result = ImportResult()
    files = []
    candidates = src.rglob("*.md") if recurse else src.glob("*.md")
    for path in sorted(candidates):
        rel_parts = path.relative_to(src).parts
        if recurse and rel_parts and rel_parts[0] in skip:
            continue  # scaffolding under --recurse: not candidates, so not skips either
        if path.name in IGNORED_NAMES:
            result.skipped.append((path, SKIP_IGNORED_NAME))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        if fm is None:
            # not a fabric entry: a dashboard, a notes file, a truncated write
            result.skipped.append((path, SKIP_UNPARSABLE))
            continue
        files.append((path, fm, body))

    # The policy is top-level only by default, and the report has to say what that policy left
    # behind: every direct subdirectory holding at least one markdown file, counted at any depth,
    # so `_stubs/` with 4080 files cannot hide behind a silent zero. Under --recurse those files
    # are candidates (minus the skipped trees), so there is nothing to leave behind.
    if not recurse:
        for child in sorted(src.iterdir()):
            if not child.is_dir() or child.name in IGNORED_NAMES or child.name.startswith("."):
                continue
            count = sum(1 for _ in child.rglob("*.md"))
            if count:
                result.directories_ignored[child.name] = count

    claimed: dict[str, str] = {}
    for path, fm, _body in files:
        # a fabric id is worth more than a path hash, so claim every usable one first
        raw = _scalar(fm.get("id"))
        if raw and _ID_RE.fullmatch(raw) and raw not in claimed:
            claimed[raw] = path.relative_to(src).as_posix()

    for path, fm, body in files:
        rel = path.relative_to(src).as_posix()
        ftype = _scalar(fm.get("type"))
        training_value = _scalar(fm.get("training_value")).lower()
        if not ftype.strip():
            # a note with no `type` has no kind the reader can trust; importing it as a default
            # would invent one (#17)
            result.skipped.append((path, SKIP_NO_TYPE))
            continue
        # A duplicate id is not a skip: `_entry_id` gives the second claimant a stable hash of its
        # own path, because dropping a file whose id a neighbour already took loses data for the
        # sake of a tidier report. The id is the corpus's; the file is still ours.
        if not body.strip():
            result.skipped.append((path, SKIP_EMPTY_BODY))
            continue
        if ftype.lower() in NOISE_TYPES and training_value == "low" and not include_noise:
            result.skipped.append((path, SKIP_NOISE))
            continue
        kind = KIND_BY_TYPE.get(ftype.lower(), DEFAULT_KIND)
        summary = _scalar(fm.get("summary"))
        if looks_like_blob(summary):
            # #77: the summary is the retrieval handle, and a blob's tokens rank as well as prose's,
            # so a JSON body here crowds real recall. The memory is the point, so derive a statement
            # from the body; refuse only when the body holds no prose at all.
            derived = first_prose_line(body)
            if not derived:
                result.skipped.append((path, SKIP_SUMMARY_NOT_PROSE))
                continue
            summary = derived
        evidence = _evidence(fm)
        importance = IMPORTANCE_BY_TRAINING_VALUE.get(training_value, 0.0)
        state = STATE_BY_TRAINING_VALUE.get(training_value, DEFAULT_STATE)

        # #30: a pasted tool transcript is not a claim. It is imported pruned, with a tag -
        # visible and reversible - and never promoted.
        transcript = looks_like_transcript(summary)
        if transcript:
            state = "pruned"
        elif state == "promoted" and not (importance >= promote_threshold and evidence):
            # #26: the same gate promote.py applies. `promoted` is a claim that the entry
            # cleared the bar; without evidence it cleared nothing.
            state = "raw"

        tags = _tags(fm, state)
        if transcript:
            tags.append(TRANSCRIPT_TAG)
        elif state == "raw" and STATE_BY_TRAINING_VALUE.get(training_value) == "promoted" \
                and not evidence:
            tags.append(NO_EVIDENCE_TAG)

        entry = Entry(
            id=_entry_id(fm, rel, claimed),
            kind=kind,
            state=state,
            agent=_scalar(fm.get("agent")),
            project=_scalar(fm.get("project_id")),
            session=_scalar(fm.get("session_id")),
            created=_created(fm.get("timestamp")),
            source=f"fabric:{rel}",
            confidence=_confidence(fm),
            importance=importance,
            summary=summary,
            evidence=evidence,
            tags=tags,
            body=_strip_preamble(body, summary),
        )
        if not dry_run:
            write_entry(root, entry)
        result.written.append(entry)
        result.by_kind[kind] = result.by_kind.get(kind, 0) + 1
        result.by_state[state] = result.by_state.get(state, 0) + 1
    return result


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a fabric file into its frontmatter mapping and its body."""
    m = _FM_RE.match(text)
    if not m:
        return None, text
    fm: dict = {}
    pending_key: str | None = None      # a quoted scalar that has not closed yet
    pending: list[str] = []
    for line in m.group(1).splitlines():
        if pending_key is not None:
            pending.append(line.strip())
            if _closes_quote(" ".join(pending)):
                fm[pending_key] = _parse_value(" ".join(pending))
                pending_key, pending = None, []
            continue
        mm = _KV_RE.match(line)
        if not mm:
            continue
        key, raw = mm.group(1), mm.group(2)
        # A quoted scalar may wrap onto following lines (YAML folding). Fabric writes summaries as
        # `summary: '... long text ...'` and its writer wraps them, so a line-based reader truncated the
        # summary at the wrap and kept the opening quote: 88 entries on the live vault, 50 of them
        # promoted, and the truncated prefix made 20 unrelated entries look near-identical (#54).
        if raw[:1] in ("'", '"') and not _closes_quote(raw):
            pending_key, pending = key, [raw]
            continue
        fm[key] = _parse_value(raw)
    if pending_key is not None:         # unterminated: take what we have rather than lose the entry
        fm[pending_key] = _parse_value(" ".join(pending))
    return fm, text[m.end():]


def _closes_quote(raw: str) -> bool:
    """Does this value's opening quote get closed on the same logical line?

    Only the first character decides the quote style: a summary that merely *contains* an apostrophe
    ("the agent's box") is not a quoted scalar and must not be treated as an unterminated one.
    """
    raw = raw.strip()
    if not raw or raw[0] not in "\"'":
        return True
    quote = raw[0]
    if quote == '"':
        try:
            json.JSONDecoder().raw_decode(raw)
            return True
        except ValueError:
            pass
    # single-quoted YAML escapes a literal quote by doubling it, so count pairs
    body, i = raw[1:], 0
    while i < len(body):
        if body[i] == quote:
            if quote == "'" and i + 1 < len(body) and body[i + 1] == "'":
                i += 2
                continue
            return True
        if quote == '"' and body[i] == "\\":
            i += 2
            continue
        i += 1
    return False


def _parse_value(raw: str):
    """Read one frontmatter value: a JSON-ish string, an inline list, or a bare scalar."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] == '"':
        return _parse_quoted(raw)
    if raw[0] == "'":
        return _parse_single_quoted(raw)
    if raw[0] == "[":
        return _parse_inline_list(raw)
    return _strip_comment(raw).strip()


def _parse_single_quoted(raw: str) -> str:
    """A YAML single-quoted scalar: the only escape is `''` for a literal apostrophe."""
    body = raw.strip()
    if len(body) >= 2 and body.endswith("'"):
        body = body[:-1]
    return body[1:].replace("''", "'")


def _parse_quoted(raw: str) -> str:
    """A double-quoted value: JSON escapes and all (`\\u2014` is an em dash, not six letters)."""
    try:
        value, end = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        value, end = None, 0
    if isinstance(value, str) and _only_comment_follows(raw[end:]):
        return value
    # hand-written quotes that are not valid JSON: keep the text, undo the escaping
    m = re.match(r'"(.*)"\s*(?:#.*)?$', raw, re.S)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return raw.strip('"')


def _only_comment_follows(rest: str) -> bool:
    rest = rest.strip()
    return not rest or rest.startswith("#")


def _parse_inline_list(raw: str) -> list[str]:
    """`[a,b]` or `["a","b"]` -> ["a", "b"]."""
    inner = raw.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    items: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in inner:
        if ch == '"':
            in_quote = not in_quote
            current.append(ch)
        elif ch == "," and not in_quote:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    out: list[str] = []
    for item in items:
        item = item.strip()
        if len(item) >= 2 and item[0] == '"' and item[-1] == '"':
            try:
                item = json.loads(item)
            except ValueError:
                item = item[1:-1]
        if item:
            out.append(item)
    return out


def _strip_comment(raw: str) -> str:
    """Drop a trailing `# comment` from a bare scalar."""
    idx = raw.find(" #")
    return raw[:idx] if idx != -1 else raw


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _evidence(fm: dict) -> str:
    """`outcome` plus `evidence` -- what happened, and the proof it happened.

    Folded onto one line: the store writes `**Evidence:** {evidence}` into the body, so a
    multi-line value would break that line and leave the tail behind as stray body text.
    """
    parts = [_one_line(_scalar(fm.get("outcome"))), _one_line(_scalar(fm.get("evidence")))]
    return " — ".join(p for p in parts if p)


def _one_line(text: str) -> str:
    """Collapse whitespace runs so the value stays on a single line."""
    return " ".join(text.split())


def _tags(fm: dict, state: str) -> list[str]:
    tags = fm.get("tags")
    out = [str(t).strip() for t in tags] if isinstance(tags, list) else []
    out = [t for t in out if t]
    if state == "pruned" and PRUNED_TAG not in out:
        out.append(PRUNED_TAG)
    return out


def _confidence(fm: dict) -> float:
    """fabric `verified` -> store `confidence`; absent keeps the store default."""
    raw = _scalar(fm.get("verified")).lower()
    if raw in CONFIDENCE_BY_VERIFIED:
        return CONFIDENCE_BY_VERIFIED[raw]
    return 0.5


def _created(value) -> str:
    """The fabric timestamp, normalised to the ISO 8601 Z the store writes."""
    ts = _scalar(value)
    if not ts or _ISO_Z_RE.match(ts):
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_preamble(body: str, summary: str) -> str:
    """The body without the H1/summary preamble -- the store writes that part itself."""
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("# "):
        lines.pop(0)
    if summary and lines and lines[0].strip() == summary.strip():
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _entry_id(fm: dict, rel: str, claimed: dict[str, str]) -> str:
    """The fabric id when it is usable and free, else a stable hash of the source path."""
    raw = _scalar(fm.get("id"))
    if raw and _ID_RE.fullmatch(raw) and claimed.get(raw) == rel:
        return raw
    candidate = _path_hash(rel)
    salt = 0
    while candidate in claimed:
        salt += 1
        candidate = _path_hash(f"{rel}#{salt}")
    claimed[candidate] = rel
    return candidate


def _path_hash(rel: str) -> str:
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:8]
